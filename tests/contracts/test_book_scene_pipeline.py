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
        "book_ingest", "scene_scout", "character_bible", "proposal", "scene_plan", "assets", "edit", "compose"]


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


def test_ingest_docx_pages_and_images(tmp_path):
    import zipfile
    _png(tmp_path / "pic.png")
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    doc = (f'<w:document {w} {a} {r}><w:body>'
           '<w:p><w:r><w:t>אבי רעה את הבקר.</w:t></w:r></w:p>'
           '<w:p><w:r><w:drawing><a:blip r:embed="rId9"/></w:drawing></w:r></w:p>'
           '<w:p><w:r><w:t>אבי ליד הנהר</w:t></w:r></w:p>'
           '<w:p><w:r><w:br w:type="page"/><w:t>הורג הלטאות!</w:t></w:r></w:p>'
           '</w:body></w:document>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId9" Target="media/image1.png"/></Relationships>')
    book = tmp_path / "ספר.docx"
    with zipfile.ZipFile(book, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
        z.write(tmp_path / "pic.png", "word/media/image1.png")
    res = BookIngest().execute({"operation": "ingest", "book_path": str(book), "book_id": "docx-book"})
    assert res.success, res.error
    src = res.data["book_source"]
    assert src["language"] == "he" and src["page_count"] == 2
    assert src["images"] and src["images"][0]["context"] == "אבי ליד הנהר"
    page2 = BookIngest().execute({"operation": "pages", "book_id": "docx-book", "page_start": 2}).data["text"]
    assert "הורג הלטאות" in page2 and "הבקר" not in page2


# ---------------------------------------------------------------- scout + assemble

def _ingest_text(tmp_path, text: str, book_id="scout-book"):
    p = tmp_path / "b.txt"
    p.write_text(text, encoding="utf-8")
    assert BookIngest().execute({"operation": "ingest", "book_path": str(p), "book_id": book_id}).success
    return book_id


HEB = ("פרק א\nיום אחד, בזמן שרעה את הבקר, עברה לידו לטאה גדולה. הוא התקרב אליה עם מקלו.\n"
       "פרק ב\nזמן קצר לפני שנפטר, הגיע לביתי, מסר לי יומן ואמר לי: \"מתישהו תכתוב עליי\".\n"
       "צגאיה אשתה: \"באופן אישי קראתי לך 'אור'.\"\n") * 3


def test_sections_cover_the_whole_book(tmp_path):
    bid = _ingest_text(tmp_path, HEB)
    res = BookIngest().execute({"operation": "sections", "book_id": bid, "section_chars": 150})
    assert res.success and res.data["count"] > 1
    full = (book_paths.BOOKS_DIR / bid / "text.txt").read_text(encoding="utf-8")
    joined = "".join(BookIngest().execute({"operation": "sections", "book_id": bid, "section_chars": 150,
                                           "section": i}).data["text"] for i in range(1, res.data["count"] + 1))
    assert joined == full


def test_passage_is_verbatim_and_tolerates_niqqud_and_quote_styles(tmp_path):
    bid = _ingest_text(tmp_path, HEB)
    res = BookIngest().execute({"operation": "passage", "book_id": bid,
                                "start_quote": "יוֹם אחד, בזמן", "end_quote": "עם מקלו."})
    assert res.success, res.error
    assert res.data["text"].startswith("יום אחד") and res.data["text"].endswith("עם מקלו.")
    res = BookIngest().execute({"operation": "passage", "book_id": bid,
                                "start_quote": "ואמר לי: “מתישהו", "end_quote": "עליי”."})
    assert res.success, res.error


def test_verify_candidates_rejects_invented_quotes(tmp_path):
    bid = _ingest_text(tmp_path, HEB)
    good = {"id": "c1", "title": "הורג הלטאות", "start_quote": "יום אחד, בזמן שרעה", "end_quote": "עם מקלו.",
            "page_start": 1, "page_end": 1, "summary": "...", "characters": ["אבי"], "visual_score": 5,
            "est_shots": 4, "est_seconds": 25, "readable_text": []}
    bad = dict(good, id="c2", end_quote="והלטאה ברחה ליער.", readable_text=["משפט שלא בספר"])
    sc = {"version": "1.0", "book_id": bid, "candidates": [good, bad], "selected": ["c1", "c9"]}
    res = BookIngest().execute({"operation": "verify_candidates", "book_id": bid, "scene_candidates": sc})
    assert res.success, res.error
    issues = " | ".join(res.data["issues"])
    assert not res.data["passed"]
    assert "c2: start_quote/end_quote" in issues and "c2: readable_text" in issues and "c9" in issues
    assert "c1:" not in issues


def test_text_overlays_must_be_verbatim_from_the_book(tmp_path):
    bid = _ingest_text(tmp_path, HEB, book_id="little-book")
    tool = CharacterBible()
    tool.execute({"operation": "save", "book_id": bid, "character_bible": _bible(bid, str(_png(tmp_path / "a.png")))})
    sl = _shot_list()
    sl["passage"]["text"] = "Anna opened the door and stepped into the rain."
    sl["shots"][0]["text_overlays"] = [{"text": "באופן אישי קראתי לך 'אור'.", "attribution": "צגאיה אשתה"}]
    assert tool.execute({"operation": "check_shot_list", "book_id": bid, "book_shot_list": sl}).data["passed"]
    sl["shots"][0]["text_overlays"][0]["text"] = "מילים שאף אחד לא כתב"
    res = tool.execute({"operation": "check_shot_list", "book_id": bid, "book_shot_list": sl})
    assert not res.data["passed"] and "text_overlay" in res.data["issues"][0]


def test_assemble_outputs_final_clean_and_matching_srt(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import tools.book.book_scene_assemble as asm
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    monkeypatch.setattr(asm, "PROJECTS_DIR", tmp_path / "projects")
    root = tmp_path / "projects" / "demo"
    shots = root / "assets" / "shots"
    shots.mkdir(parents=True)
    for sid, color in (("s1", "red"), ("s2", "blue")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d=2:r=24",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(shots / f"{sid}.mp4")], check=True)
    passage = "יום אחד, בזמן שרעה את הבקר. הוא התקרב אליה עם מקלו."
    sl = {"version": "1.0", "book_id": "x", "passage": {"text": passage, "page_start": 1, "page_end": 1},
          "shots": [{"id": "s1", "scene_id": "s1", "source_quote": "יום אחד, בזמן שרעה את הבקר.", "character_ids": [],
                     "video_prompt": "field", "duration_seconds": 3},
                    {"id": "s2", "scene_id": "s2", "source_quote": "הוא התקרב אליה עם מקלו.", "character_ids": [],
                     "video_prompt": "stick", "duration_seconds": 3,
                     "text_overlays": [{"text": "באופן אישי קראתי לך 'אור'.", "attribution": "צגאיה אשתה", "seconds": 2}]}],
          "fidelity_review": {"score": 9, "approved": True, "issues": []}}
    (root / "book_shot_list.json").write_text(json.dumps(sl, ensure_ascii=False), encoding="utf-8")
    res = asm.BookSceneAssemble().execute({"project_id": "demo", "order": ["s2", "s1"], "resolution": "320x180",
                                           "end_card": {"title": "סוף", "seconds": 1.5}})
    assert res.success, res.error
    d = res.data
    assert d["order"] == ["s2", "s1"]
    assert d["duration_seconds"] == pytest.approx(d["clean_duration_seconds"] + 1.5, abs=0.2)
    srt = Path(d["srt"]).read_text(encoding="utf-8-sig")
    blocks = [b for b in srt.strip().split("\n\n") if b]
    assert len(blocks) == 2
    assert "הוא התקרב" in blocks[0] and "יום אחד" in blocks[1]          # follows the requested order
    assert all(line.startswith("‏") and line.endswith("‏")      # RTL marks on every caption line
               for b in blocks for line in b.split("\n")[2:])
    last_end = blocks[-1].split("\n")[1].split(" --> ")[1]
    h, m, rest = last_end.split(":")
    assert float(rest.replace(",", ".")) <= d["clean_duration_seconds"]   # SRT fits the clean cut


def test_render_tool_refuses_a_shot_list_that_fails_the_gate(tmp_path, monkeypatch):
    import tools.book.book_shot_render as rnd
    monkeypatch.setattr(rnd, "PROJECTS_DIR", tmp_path / "projects")
    tool = CharacterBible()
    tool.execute({"operation": "save", "book_id": "little-book",
                  "character_bible": _bible("little-book", str(_png(tmp_path / "a.png")), locked=False)})
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    (root / "book_shot_list.json").write_text(json.dumps(_shot_list(), ensure_ascii=False), encoding="utf-8")
    res = rnd.BookShotRender().execute({"project_id": "p", "book_id": "little-book"})
    assert not res.success and "fidelity gate" in res.error and "not locked" in res.error
