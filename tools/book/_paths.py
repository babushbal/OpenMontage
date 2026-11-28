"""Book library location. Books live outside a single project so that the same
character references are reused by every scene made from that book."""
from __future__ import annotations

import os
import re
from pathlib import Path

from lib.paths import REPO_ROOT

BOOKS_DIR = Path(os.environ.get("OPENMONTAGE_BOOKS_DIR") or (REPO_ROOT / "books"))


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "book"


def book_dir(book_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", book_id):
        raise ValueError(f"invalid book_id {book_id!r}")
    return BOOKS_DIR / book_id
