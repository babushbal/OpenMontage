"""Ingest a PDF / EPUB / DOCX / TXT book into text with page markers plus its illustrations.

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


_CHAR_MAP = {"״": '"', "׳": "'", "“": '"', "”": '"',
             "‘": "'", "’": "'", "–": "-", "—": "-"}


def _norm_chars(text: str) -> list[str | None]:
    """Per-character version of character_bible._norm: one entry per raw char, None when the
    char disappears (niqqud, repeated whitespace, page markers). Lets a match in normalised
    text be mapped back to the exact raw span."""
    masked = re.sub(r"\[\[page \d+\]\]", lambda m: " " * len(m.group()), text)
    out: list[str | None] = []
    prev_space = True
    for ch in masked:
        if "֑" <= ch <= "ׇ":
            out.append(None)
            continue
        ch = _CHAR_MAP.get(ch, ch)
        if ch.isspace():
            out.append(None if prev_space else " ")
            prev_space = True
        else:
            out.append(ch)
            prev_space = False
    return out


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


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _docx(path: Path, img_dir: Path) -> tuple[list[str], list[dict]]:
    """Word .docx without extra dependencies. Pages follow Word's own page breaks
    (explicit breaks and the last-rendered layout); a document saved without layout
    info comes out as one page."""
    import zipfile
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as z:
        body = ET.fromstring(z.read("word/document.xml")).find(f"{_W}body")
        rels = {}
        if "word/_rels/document.xml.rels" in z.namelist():
            for rel in ET.fromstring(z.read("word/_rels/document.xml.rels")):
                rels[rel.get("Id")] = rel.get("Target", "")
        pages: list[list[str]] = [[]]
        images: list[dict] = []
        for para in body.iter(f"{_W}p"):
            parts = []
            for node in para.iter():
                if node.tag in (f"{_W}lastRenderedPageBreak",) or (
                        node.tag == f"{_W}br" and node.get(f"{_W}type") == "page"):
                    if pages[-1] or parts:
                        pages[-1].append("".join(parts))
                        parts = []
                        pages.append([])
                elif node.tag == f"{_W}t" and node.text:
                    parts.append(node.text)
                elif node.tag == f"{_W}tab":
                    parts.append("\t")
                elif node.tag == f"{_A}blip":
                    target = rels.get(node.get(f"{_R}embed"), "")
                    member = "word/" + target.lstrip("/").removeprefix("word/")
                    if member not in z.namelist():
                        continue
                    img_id = f"d{len(pages)}_{len(images)}"
                    out = img_dir / f"{img_id}{Path(target).suffix or '.png'}"
                    out.write_bytes(z.read(member))
                    try:
                        w, h = _img_size(out)
                    except Exception:
                        out.unlink()
                        continue
                    if min(w, h) < MIN_IMAGE_SIDE:
                        out.unlink()
                        continue
                    images.append({"id": img_id, "path": str(out), "page": len(pages),
                                   "width": w, "height": h, "context": ""})
            text = "".join(parts)
            pages[-1].append(text)
            for img in images:  # caption = first non-empty paragraph after the picture
                if not img["context"] and img["page"] == len(pages) and text.strip():
                    img["context"] = text.strip()[:600]
    return ["\n".join(p for p in page if p.strip()) for page in pages], images


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
        "turning an uploaded PDF/EPUB/DOCX/TXT book into page-marked text",
        "extracting a book's character illustrations for identity references",
    ]
    not_good_for = ["scanned image-only PDFs without a text layer (run OCR first)"]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": ["ingest", "pages", "sections", "passage", "verify_candidates"]},
            "book_path": {"type": "string", "description": "ingest: path to .pdf/.epub/.docx/.txt/.md"},
            "book_id": {"type": "string", "description": "Optional stable slug; defaults to the file name"},
            "page_start": {"type": "integer", "description": "pages: first page"},
            "page_end": {"type": "integer", "description": "pages: last page (inclusive)"},
            "section_chars": {"type": "integer", "description": "sections: target size of each reading chunk (default 20000)"},
            "section": {"type": "integer", "description": "sections: return the text of this section (1-based); omit for the index"},
            "start_quote": {"type": "string", "description": "passage: verbatim first words"},
            "end_quote": {"type": "string", "description": "passage: verbatim last words"},
            "scene_candidates": {"type": "object", "description": "verify_candidates: the scout's artifact"},
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
            op = inputs.get("operation")
            if op == "pages":
                return self._pages(inputs, start)
            if op == "sections":
                return self._sections(inputs, start)
            if op == "passage":
                return self._passage(inputs, start)
            if op == "verify_candidates":
                return self._verify_candidates(inputs, start)
            return self._ingest(inputs, start)
        except Exception as exc:  # noqa: BLE001 - surfaced to the agent
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _text(book_id: str) -> str:
        return (book_dir(book_id) / "text.txt").read_text(encoding="utf-8")

    def _sections(self, inputs, start) -> ToolResult:
        """Split the book into reading chunks on paragraph boundaries so a scout can read
        a long book section by section without losing its place."""
        text = self._text(inputs["book_id"])
        size = int(inputs.get("section_chars") or 20000)
        bounds, pos = [], 0
        while pos < len(text):
            end = min(len(text), pos + size)
            if end < len(text):
                cut = text.rfind("\n", pos + size // 2, end)
                end = cut + 1 if cut > 0 else end
            bounds.append((pos, end))
            pos = end
        index = []
        for i, (a, b) in enumerate(bounds, start=1):
            pages = [int(m) for m in re.findall(r"\[\[page (\d+)\]\]", text[:b])]
            first = ([int(m) for m in re.findall(r"\[\[page (\d+)\]\]", text[:a + 1])] or [1])[-1]
            index.append({"section": i, "chars": b - a, "page_start": first,
                          "page_end": pages[-1] if pages else first,
                          "opening": text[a:a + 120].strip().replace("\n", " ")})
        data: dict[str, Any] = {"sections": index, "count": len(index)}
        if inputs.get("section"):
            a, b = bounds[int(inputs["section"]) - 1]
            data["text"] = text[a:b]
        return ToolResult(success=True, data=data, duration_seconds=time.time() - start)

    @staticmethod
    def _locate(text: str, start_quote: str, end_quote: str) -> tuple[int, int] | None:
        from tools.book.character_bible import _norm
        # map normalised positions back to the raw text so the passage is returned verbatim
        raw_idx, norm_chars = [], []
        for i, ch in enumerate(_norm_chars(text)):
            if ch is not None:
                raw_idx.append(i)
                norm_chars.append(ch)
        norm = "".join(norm_chars)
        a = norm.find(_norm(start_quote))
        if a < 0:
            return None
        b = norm.find(_norm(end_quote), a)
        if b < 0:
            return None
        b_end = b + len(_norm(end_quote)) - 1
        return raw_idx[a], raw_idx[b_end] + 1

    def _passage(self, inputs, start) -> ToolResult:
        text = self._text(inputs["book_id"])
        span = self._locate(text, inputs["start_quote"], inputs["end_quote"])
        if span is None:
            return ToolResult(success=False, error="start_quote/end_quote not found verbatim (in that order) in the book")
        a, b = span
        passage = re.sub(r"\n?\[\[page \d+\]\]\n?", "\n", text[a:b]).strip()
        pages = [int(m) for m in re.findall(r"\[\[page (\d+)\]\]", text[:b])] or [1]
        first = ([int(m) for m in re.findall(r"\[\[page (\d+)\]\]", text[:a + 1])] or [1])[-1]
        return ToolResult(success=True, data={"text": passage, "page_start": first, "page_end": pages[-1],
                                              "chars": len(passage)}, duration_seconds=time.time() - start)

    def _verify_candidates(self, inputs, start) -> ToolResult:
        from schemas.artifacts import validate_artifact as _validate
        sc = inputs["scene_candidates"]
        _validate("scene_candidates", sc)
        text = self._text(inputs["book_id"])
        from tools.book.character_bible import _norm
        norm_text = _norm(re.sub(r"\[\[page \d+\]\]", " ", text))
        issues, ids = [], set()
        for c in sc["candidates"]:
            if c["id"] in ids:
                issues.append(f"{c['id']}: duplicate id")
            ids.add(c["id"])
            if self._locate(text, c["start_quote"], c["end_quote"]) is None:
                issues.append(f"{c['id']}: start_quote/end_quote not found verbatim and in order")
            for t in c.get("readable_text", []):
                if _norm(t) not in norm_text:
                    issues.append(f"{c['id']}: readable_text is not verbatim: {t[:40]}...")
        for sid in sc.get("selected", []):
            if sid not in ids:
                issues.append(f"selected id {sid!r} is not a candidate")
        return ToolResult(success=True, data={"passed": not issues, "issues": issues},
                          duration_seconds=time.time() - start)

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
        elif ext == ".docx":
            pages, images = _docx(src, img_dir)
        elif ext in (".txt", ".md"):
            pages, images = [src.read_text(encoding="utf-8")], []
        else:
            return ToolResult(success=False, error=f"Unsupported format {ext}; use pdf, epub, docx or txt")

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
