"""Ingest a PDF / EPUB / TXT book into text with page markers plus its illustrations.

Output (under books/<book_id>/):
  text.txt            full text, every page prefixed with [[page N]]
  images/<id>.<ext>   every embedded illustration big enough to depict a person
  book_source.json    the book_source artifact
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from schemas.artifacts import validate_artifact
from tools.base_tool import (
    BaseTool, Determinism, ExecutionMode, ResourceProfile, RetryPolicy,
    ToolResult, ToolRuntime, ToolStability, ToolStatus, ToolTier,
)
from tools.book._paths import book_dir, slugify

MIN_IMAGE_SIDE = 200  # skip ornaments, bullets, dingbats

_SCRIPTS = {
    "he": r"[֐-׿]",
    "ar": r"[؀-ۿ]",
    "ru": r"[Ѐ-ӿ]",
    "en": r"[A-Za-z]",
}


def detect_language(text: str) -> str:
    sample = text[:200_000]
    counts = {lang: len(re.findall(rx, sample)) for lang, rx in _SCRIPTS.items()}
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    return lang if n else "unknown"


def pages_slice(text: str, first: int, last: int) -> str:
    """Text of pages first..last (inclusive) from a [[page N]]-marked text."""
    m1 = re.search(rf"\[\[page {first}\]\]", text)
    if not m1:
        raise ValueError(f"page {first} not found")
    m2 = re.search(rf"\[\[page {last + 1}\]\]", text)
    return text[m1.start(): m2.start() if m2 else len(text)]


def _img_size(path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def _pdf(path: Path, img_dir: Path) -> tuple[list[str], list[dict]]:
    import pymupdf

    doc = pymupdf.open(path)
    pages, images = [], []
    for pno, page in enumerate(doc, start=1):
        page_text = page.get_text("text")
        pages.append(page_text)
        for n, info in enumerate(page.get_images(full=True)):
            xref = info[0]
            try:
                data = doc.extract_image(xref)
            except Exception:
                continue
            w, h = data.get("width", 0), data.get("height", 0)
            if min(w, h) < MIN_IMAGE_SIDE:
                continue
            img_id = f"p{pno}_{n}"
            out = img_dir / f"{img_id}.{data['ext']}"
            out.write_bytes(data["image"])
            images.append({"id": img_id, "path": str(out), "page": pno, "width": w, "height": h,
                           "context": _pdf_caption(pymupdf, page, xref, page_text)})
    return pages, images


def _pdf_caption(pymupdf, page, xref, page_text: str) -> str:
    try:
        rects = page.get_image_rects(xref)
    except Exception:
        rects = []
    if rects:
        r = rects[0]
        near = pymupdf.Rect(r.x0 - 20, r.y0 - 60, r.x1 + 20, r.y1 + 80)
        blocks = [b[4].strip() for b in page.get_text("blocks") if pymupdf.Rect(b[:4]).intersects(near)]
        cap = " ".join(b for b in blocks if b)
        if cap:
            return cap[:600]
    return page_text.strip()[:600]


def _epub(path: Path, img_dir: Path) -> tuple[list[str], list[dict]]:
    from bs4 import BeautifulSoup
    from ebooklib import ITEM_DOCUMENT, epub

    book = epub.read_epub(str(path))
    by_name = {Path(i.get_name()).name: i for i in book.get_items()}
    pages, images = [], []
    for sec, item in enumerate(book.get_items_of_type(ITEM_DOCUMENT), start=1):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for n, img in enumerate(soup.find_all(["img", "image"])):
            src = img.get("src") or img.get("xlink:href") or img.get("href") or ""
            res = by_name.get(Path(src).name)
            if res is None:
                continue
            img_id = f"s{sec}_{n}"
            out = img_dir / f"{img_id}{Path(src).suffix or '.png'}"
            out.write_bytes(res.get_content())
            try:
                w, h = _img_size(out)
            except Exception:
                out.unlink()
                continue
            if min(w, h) < MIN_IMAGE_SIDE:
                out.unlink()
                continue
            parent = img.find_parent()
            ctx = " ".join(filter(None, [img.get("alt", ""), parent.get_text(" ", strip=True) if parent else ""]))
            images.append({"id": img_id, "path": str(out), "page": sec, "width": w, "height": h,
                           "context": ctx[:600]})
        pages.append(soup.get_text("\n", strip=True))
    return pages, images


class BookIngest(BaseTool):
    name = "book_ingest"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "analysis"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["python:pymupdf", "python:ebooklib", "python:bs4"]
    install_instructions = "pip install pymupdf ebooklib beautifulsoup4"

    capabilities = ["book_ingest", "extract_book_text", "extract_book_illustrations", "page_slice"]
    best_for = [
        "turning an uploaded PDF/EPUB/TXT book into page-marked text",
        "extracting a book's character illustrations for identity references",
    ]
    not_good_for = ["scanned image-only PDFs without a text layer (run OCR first)"]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": ["ingest", "pages"]},
            "book_path": {"type": "string", "description": "ingest: path to .pdf/.epub/.txt/.md"},
            "book_id": {"type": "string", "description": "Optional stable slug; defaults to the file name"},
            "page_start": {"type": "integer", "description": "pages: first page"},
            "page_end": {"type": "integer", "description": "pages: last page (inclusive)"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=False)
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = ["operation", "book_path", "book_id", "page_start", "page_end"]
    side_effects = ["writes books/<book_id>/"]

    def get_status(self) -> ToolStatus:
        try:
            import pymupdf  # noqa: F401
            return ToolStatus.AVAILABLE
        except ImportError:
            return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        try:
            if inputs.get("operation") == "pages":
                return self._pages(inputs, start)
            return self._ingest(inputs, start)
        except Exception as exc:  # noqa: BLE001 - surfaced to the agent
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")

    def _pages(self, inputs, start) -> ToolResult:
        root = book_dir(inputs["book_id"])
        text = (root / "text.txt").read_text(encoding="utf-8")
        first = int(inputs["page_start"])
        last = int(inputs.get("page_end") or first)
        return ToolResult(success=True, data={"text": pages_slice(text, first, last)},
                          duration_seconds=time.time() - start)

    def _ingest(self, inputs, start) -> ToolResult:
        src = Path(inputs["book_path"]).expanduser()
        if not src.exists():
            return ToolResult(success=False, error=f"File not found: {src}")
        book_id = inputs.get("book_id") or slugify(src.stem)
        root = book_dir(book_id)
        img_dir = root / "images"
        img_dir.mkdir(parents=True, exist_ok=True)

        ext = src.suffix.lower()
        if ext == ".pdf":
            pages, images = _pdf(src, img_dir)
        elif ext == ".epub":
            pages, images = _epub(src, img_dir)
        elif ext in (".txt", ".md"):
            pages, images = [src.read_text(encoding="utf-8")], []
        else:
            return ToolResult(success=False, error=f"Unsupported format {ext}; use pdf, epub or txt")

        text = "".join(f"\n[[page {i}]]\n{p}" for i, p in enumerate(pages, start=1))
        if len(text.strip()) < 50 and images:
            return ToolResult(success=False, error="No text layer found - this looks like a scanned PDF. Run OCR first.")
        text_path = root / "text.txt"
        text_path.write_text(text, encoding="utf-8")

        artifact = {
            "version": "1.0",
            "book_id": book_id,
            "title": src.stem,
            "source_path": str(src.resolve()),
            "text_path": str(text_path),
            "language": detect_language(text),
            "page_count": len(pages),
            "char_count": len(text),
            "images": images,
        }
        validate_artifact("book_source", artifact)
        out = root / "book_source.json"
        out.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        return ToolResult(
            success=True,
            data={"book_source": artifact, "book_source_path": str(out)},
            artifacts=[str(out), str(text_path)] + [i["path"] for i in images],
            duration_seconds=time.time() - start,
        )
