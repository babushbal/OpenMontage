"""Book-level character bible: the identity lock that keeps characters consistent.

The bible lives at books/<book_id>/character_bible.json and is shared by every
scene made from the book, so a character looks the same in scene 1 and scene 40.

Operations
  load              -> current bible (or an empty skeleton)
  save              -> validate + write; references of locked characters are immutable
  import_reference  -> copy an image (book illustration, user upload, generated) into the bible
  kling_elements    -> Kling O3 `elements` payload for an ordered list of character ids
  check_shot_list   -> deterministic fidelity/consistency gate for a book_shot_list
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
import time
from pathlib import Path
from typing import Any

from schemas.artifacts import validate_artifact
from tools.base_tool import (
    BaseTool, Determinism, ExecutionMode, ResourceProfile, RetryPolicy,
    ToolResult, ToolRuntime, ToolStability, ToolStatus, ToolTier,
)
from tools.book._paths import book_dir

ELEMENT_TAG = re.compile(r"@Element(\d+)")


def _norm(s: str) -> str:
    # unify whitespace, quotes and Hebrew/Arabic punctuation variants for substring checks
    s = s.replace("״", '"').replace("׳", "'").replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"[֑-ׇ]", "", s)  # Hebrew niqqud / cantillation
    return re.sub(r"\s+", " ", s).strip()


def _data_uri(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"


class CharacterBible(BaseTool):
    name = "character_bible"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "character_consistency"
    provider = "openmontage"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    dependencies = []

    capabilities = ["character_bible", "identity_lock", "kling_elements", "shot_list_fidelity_check"]
    best_for = [
        "keeping book characters visually identical across shots and scenes",
        "binding characters to Kling O3 @ElementN reference slots",
        "checking that every shot quotes the source passage verbatim",
    ]

    input_schema = {
        "type": "object",
        "required": ["operation", "book_id"],
        "properties": {
            "operation": {"type": "string",
                          "enum": ["load", "save", "import_reference", "kling_elements", "check_shot_list"]},
            "book_id": {"type": "string"},
            "character_bible": {"type": "object", "description": "save: full bible"},
            "unlock": {"type": "boolean", "description": "save: allow changing references of locked characters (only on explicit user request)"},
            "character_id": {"type": "string"},
            "image_path": {"type": "string"},
            "slot": {"type": "string", "enum": ["frontal", "angle"]},
            "origin": {"type": "string", "enum": ["book_illustration", "user_supplied", "generated_from_text"]},
            "character_ids": {"type": "array", "items": {"type": "string"}},
            "book_shot_list": {"type": "object"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=100, network_required=False)
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = []
    side_effects = ["writes books/<book_id>/character_bible.json and characters/"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    # ------------------------------------------------------------------ helpers
    def _path(self, book_id: str) -> Path:
        return book_dir(book_id) / "character_bible.json"

    def _load(self, book_id: str) -> dict:
        p = self._path(book_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {"version": "1.0", "book_id": book_id,
                "visual_style": {"description": ""}, "characters": []}

    def _write(self, book_id: str, bible: dict) -> Path:
        validate_artifact("character_bible", bible)
        p = self._path(book_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(bible, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @staticmethod
    def _by_id(bible: dict) -> dict[str, dict]:
        return {c["id"]: c for c in bible.get("characters", [])}

    # --------------------------------------------------------------- execute
    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        op = inputs.get("operation")
        try:
            handler = {
                "load": self._op_load, "save": self._op_save,
                "import_reference": self._op_import, "kling_elements": self._op_elements,
                "check_shot_list": self._op_check,
            }[op]
        except KeyError:
            return ToolResult(success=False, error=f"unknown operation {op!r}")
        try:
            result = handler(inputs)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")
        result.duration_seconds = time.time() - start
        return result

    def _op_load(self, inputs) -> ToolResult:
        bible = self._load(inputs["book_id"])
        return ToolResult(success=True, data={"character_bible": bible, "path": str(self._path(inputs["book_id"]))})

    def _op_save(self, inputs) -> ToolResult:
        book_id = inputs["book_id"]
        new = inputs["character_bible"]
        if new.get("book_id") != book_id:
            raise ValueError("character_bible.book_id does not match book_id")
        old = self._by_id(self._load(book_id))
        if not inputs.get("unlock"):
            for cid, before in old.items():
                if not before.get("locked"):
                    continue
                after = self._by_id(new).get(cid)
                if after is None:
                    raise ValueError(f"character {cid!r} is locked and cannot be removed")
                if after.get("references") != before.get("references"):
                    raise ValueError(f"character {cid!r} is locked; its references are immutable "
                                     "(pass unlock=true only if the user asked to recast)")
        for c in new.get("characters", []):
            for ref in [c["references"]["frontal"], *c["references"].get("angles", [])]:
                if ref and not Path(ref).exists():
                    raise ValueError(f"{c['id']}: reference image not found: {ref}")
        p = self._write(book_id, new)
        return ToolResult(success=True, data={"path": str(p), "characters": len(new["characters"])},
                          artifacts=[str(p)])

    def _op_import(self, inputs) -> ToolResult:
        book_id, cid = inputs["book_id"], inputs["character_id"]
        src = Path(inputs["image_path"])
        if not src.exists():
            raise FileNotFoundError(src)
        bible = self._load(book_id)
        char = self._by_id(bible).get(cid)
        if char is None:
            raise ValueError(f"character {cid!r} not in bible - save it first")
        if char.get("locked") and not inputs.get("unlock"):
            raise ValueError(f"character {cid!r} is locked")
        dest_dir = book_dir(book_id) / "characters" / cid
        dest_dir.mkdir(parents=True, exist_ok=True)
        slot = inputs.get("slot", "frontal")
        n = len(char["references"].get("angles", [])) if slot == "angle" else 0
        dest = dest_dir / (f"frontal{src.suffix}" if slot == "frontal" else f"angle_{n + 1}{src.suffix}")
        shutil.copyfile(src, dest)
        if slot == "frontal":
            char["references"]["frontal"] = str(dest)
            if inputs.get("origin"):
                char["reference_origin"] = inputs["origin"]
        else:
            angles = char["references"].setdefault("angles", [])
            if len(angles) >= 3:
                raise ValueError("Kling accepts at most 3 extra angles per element")
            angles.append(str(dest))
        p = self._write(book_id, bible)
        return ToolResult(success=True, data={"path": str(dest), "bible_path": str(p)}, artifacts=[str(dest)])

    def _op_elements(self, inputs) -> ToolResult:
        bible = self._by_id(self._load(inputs["book_id"]))
        elements, binding = [], {}
        for i, cid in enumerate(inputs.get("character_ids") or [], start=1):
            char = bible.get(cid)
            if char is None:
                raise ValueError(f"unknown character {cid!r}")
            if not char.get("locked"):
                raise ValueError(f"character {cid!r} is not locked - get user approval of its references first")
            refs = char["references"]
            angles = [a for a in refs.get("angles", []) if a] or [refs["frontal"]]
            elements.append({
                "frontal_image_url": _data_uri(refs["frontal"]),
                "reference_image_urls": [_data_uri(a) for a in angles[:3]],
            })
            binding[f"@Element{i}"] = cid
        if len(elements) > 4:
            raise ValueError("Kling O3 accepts at most 4 elements + reference images per request")
        return ToolResult(success=True, data={"elements": elements, "binding": binding})

    def _op_check(self, inputs) -> ToolResult:
        """Deterministic part of the fidelity review. The director's critic covers the rest."""
        bible = self._by_id(self._load(inputs["book_id"]))
        sl = inputs["book_shot_list"]
        validate_artifact("book_shot_list", sl)
        passage = _norm(sl["passage"]["text"])
        text_path = book_dir(inputs["book_id"]) / "text.txt"
        book_text = (_norm(re.sub(r"\[\[page \d+\]\]", " ", text_path.read_text(encoding="utf-8")))
                     if text_path.exists() else None)
        issues: list[str] = []
        names = {}
        for c in bible.values():
            for n in [c["name"], *c.get("aliases", [])]:
                if n:
                    names[n] = c["id"]
        for shot in sl["shots"]:
            sid = shot["id"]
            if _norm(shot["source_quote"]) not in passage:
                issues.append(f"{sid}: source_quote is not a verbatim part of the passage")
            if shot.get("dialogue") and _norm(shot["dialogue"]) not in passage:
                issues.append(f"{sid}: dialogue is not verbatim from the passage")
            for cid in shot["character_ids"]:
                if cid not in bible:
                    issues.append(f"{sid}: unknown character {cid!r}")
                elif not bible[cid].get("locked"):
                    issues.append(f"{sid}: character {cid!r} references are not locked/approved")
            tags = {int(t) for t in ELEMENT_TAG.findall(shot["video_prompt"])}
            expected = set(range(1, len(shot["character_ids"]) + 1))
            if tags - expected:
                issues.append(f"{sid}: prompt uses @Element tags with no bound character: {sorted(tags - expected)}")
            if expected - tags:
                issues.append(f"{sid}: characters bound but never referenced in prompt: "
                              f"{[shot['character_ids'][i - 1] for i in sorted(expected - tags)]}")
            for ov in shot.get("text_overlays", []):
                if book_text is None:
                    issues.append(f"{sid}: text_overlays need the ingested book text to verify")
                elif _norm(ov["text"]) not in book_text:
                    issues.append(f"{sid}: text_overlay is not verbatim from the book: {ov['text'][:40]}...")
            for n, cid in names.items():
                if re.search(rf"\b{re.escape(n)}\b", shot["video_prompt"]):
                    issues.append(f"{sid}: prompt names {n!r} directly - use its @Element tag so identity comes from the reference")
        return ToolResult(success=True, data={"passed": not issues, "issues": issues})
