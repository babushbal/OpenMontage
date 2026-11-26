"""Book-scene pipeline: ingest, identity lock, element binding and the fidelity gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.book._paths as book_paths
from lib.checkpoint import get_pipeline_stages
from lib.pipeline_loader import load_pipeline
from tools.book.book_ingest import BookIngest, detect_language, pages_slice
from tools.book.character_bible import CharacterBible
from tools.tool_registry import registry
from tools.video.kling_o3_fal_video import KlingO3FalVideo

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def books_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(book_paths, "BOOKS_DIR", tmp_path / "books")
    return tmp_path / "books"


def _png(path: Path, color=(200, 80, 40), size=(400, 600)) -> Path:
    from PIL import Image
    Image.new("RGB", size, color).save(path)
    return path


def _pdf(tmp_path: Path) -> Path:
    import pymupdf
    img = _png(tmp_path / "hero.png")
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Chapter One. Anna had red hair and a green coat.")
    p1.insert_image(pymupdf.Rect(72, 100, 272, 400), filename=str(img))
    p1.insert_text((72, 420), "Anna by the river")
    p2 = doc.new_page()
    p2.insert_text((72, 72), "Anna opened the door and stepped into the rain.")
    out = tmp_path / "Little Book.pdf"
    doc.save(out)
    return out


def _bible(book_id: str, frontal: str, locked=True) -> dict:
    return {
        "version": "1.0", "book_id": book_id,
        "visual_style": {"description": "watercolour, 1900s village"},
        "characters": [{
            "id": "anna", "name": "Anna", "aliases": [],
            "physical_description": "young woman, red hair, green coat",
            "source_quotes": [{"quote": "Anna had red hair and a green coat.", "page": 1}],
            "reference_origin": "book_illustration",
            "references": {"frontal": frontal, "angles": []},
            "locked": locked,
        }],
    }


def _shot_list(prompt="@Element1 opens the door and steps into the rain", quote="Anna opened the door and stepped into the rain."):
    return {
        "version": "1.0", "book_id": "little-book",
        "passage": {"text": "Anna opened the door  and stepped\ninto the rain.", "page_start": 2, "page_end": 2},
        "shots": [{"id": "s1", "scene_id": "s1", "source_quote": quote, "character_ids": ["anna"],
                   "video_prompt": prompt, "duration_seconds": 5}],
        "fidelity_review": {"score": 9, "approved": True, "issues": []},
    }


def test_manifest_loads_with_custom_stages():
    assert load_pipeline("book-scene")["stages"]
    assert get_pipeline_stages("book-scene") == [
        "book_ingest", "character_bible", "proposal", "scene_plan", "assets", "edit", "compose"]


def test_manifest_skills_and_tools_exist():
    manifest = load_pipeline("book-scene")
    for skill in manifest["required_skills"] + [s["skill"] for s in manifest["stages"]]:
        assert (REPO / "skills" / f"{skill}.md").exists(), skill
    registry.discover()
    for stage in manifest["stages"]:
        for tool in stage.get("tools_available", []) + stage.get("required_tools", []):
            assert tool in registry._tools, f"{stage['name']}: {tool}"


def test_ingest_pdf_extracts_pages_and_illustration(tmp_path):
    res = BookIngest().execute({"operation": "ingest", "book_path": str(_pdf(tmp_path))})
    assert res.success, res.error
    src = res.data["book_source"]
    assert src["book_id"] == "little-book" and src["page_count"] == 2 and src["language"] == "en"
    assert len(src["images"]) == 1 and src["images"][0]["page"] == 1
    assert "river" in src["images"][0]["context"]
    pages = BookIngest().execute({"operation": "pages", "book_id": "little-book", "page_start": 2})
    assert "stepped into the rain" in pages.data["text"] and "red hair" not in pages.data["text"]


def test_hebrew_text_and_page_slice():
    text = "\n[[page 1]]\nשלום עולם\n[[page 2]]\nדנה פתחה את הדלת\n[[page 3]]\nסוף"
    assert detect_language(text) == "he"
    assert "דנה" in pages_slice(text, 2, 2) and "סוף" not in pages_slice(text, 2, 2)


def test_locked_references_are_immutable(tmp_path):
    tool = CharacterBible()
    a = _png(tmp_path / "a.png")
    b = _png(tmp_path / "b.png", color=(0, 0, 255))
    assert tool.execute({"operation": "save", "book_id": "little-book",
                         "character_bible": _bible("little-book", str(a))}).success
    res = tool.execute({"operation": "save", "book_id": "little-book",
                        "character_bible": _bible("little-book", str(b))})
    assert not res.success and "locked" in res.error
    assert tool.execute({"operation": "save", "book_id": "little-book", "unlock": True,
                         "character_bible": _bible("little-book", str(b))}).success


def test_kling_elements_bind_in_order_and_build_valid_request(tmp_path):
    tool = CharacterBible()
    tool.execute({"operation": "save", "book_id": "little-book",
                  "character_bible": _bible("little-book", str(_png(tmp_path / "a.png")))})
    res = tool.execute({"operation": "kling_elements", "book_id": "little-book", "character_ids": ["anna"]})
    assert res.success, res.error
    assert res.data["binding"] == {"@Element1": "anna"}
    el = res.data["elements"][0]
    assert el["frontal_image_url"].startswith("data:image/png;base64,")
    assert len(el["reference_image_urls"]) == 1  # Kling needs >= 1; frontal is reused
    _, op, endpoint, payload = KlingO3FalVideo().build_request({
        "model": "kling-o3-standard", "generation_mode": "reference_to_video",
        "prompt": "@Element1 opens the door", "elements": res.data["elements"],
        "duration": "5", "aspect_ratio": "16:9", "generate_audio": False,
    })
    assert endpoint.endswith("/reference-to-video") and payload["elements"] == res.data["elements"]


def test_unlocked_character_cannot_be_rendered(tmp_path):
    tool = CharacterBible()
    tool.execute({"operation": "save", "book_id": "little-book",
                  "character_bible": _bible("little-book", str(_png(tmp_path / "a.png")), locked=False)})
    res = tool.execute({"operation": "kling_elements", "book_id": "little-book", "character_ids": ["anna"]})
    assert not res.success and "not locked" in res.error


@pytest.mark.parametrize("prompt,quote,expected", [
    ("@Element1 opens the door and steps into the rain", "Anna opened the door and stepped into the rain.", []),
    ("@Element1 opens the door", "Anna opened the window.", ["not a verbatim"]),
    ("Anna opens the door", "Anna opened the door and stepped into the rain.", ["never referenced", "names 'Anna'"]),
    ("@Element1 and @Element2 talk", "Anna opened the door and stepped into the rain.", ["no bound character"]),
])
def test_check_shot_list(tmp_path, prompt, quote, expected):
    tool = CharacterBible()
    tool.execute({"operation": "save", "book_id": "little-book",
                  "character_bible": _bible("little-book", str(_png(tmp_path / "a.png")))})
    res = tool.execute({"operation": "check_shot_list", "book_id": "little-book",
                        "book_shot_list": _shot_list(prompt, quote)})
    assert res.success, res.error
    issues = " | ".join(res.data["issues"])
    assert res.data["passed"] is (not expected)
    for fragment in expected:
        assert fragment in issues, issues
