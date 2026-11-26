"""Assemble a book scene from its rendered shots.

Outputs (projects/<project_id>/renders/):
  <name>.mp4         final cut with burned-in verbatim captions (RTL-safe) and optional end card
  <name>_clean.mp4   the same cut with no captions - for DaVinci Resolve / Premiere
  <name>.<lang>.srt  captions matching the clean cut exactly (UTF-8 BOM, RTL marks)

Shots with `text_overlays` (letters, diary pages, dedications) get the real book text
composited over a softened, slowed copy of the generated footage: video models cannot
write legible Hebrew/Arabic, so readable text never comes from the model.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from lib.paths import PROJECTS_DIR
from schemas.artifacts import validate_artifact
from tools.base_tool import (
    BaseTool, Determinism, ExecutionMode, ResourceProfile, RetryPolicy,
    ToolResult, ToolRuntime, ToolStability, ToolStatus, ToolTier,
)

RLE, RLM, PDF = "‫", "‏", "‬"
RTL_LANGS = {"he", "ar", "fa", "ur", "yi"}

_STYLE_FMT = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
              "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
              "Alignment, MarginL, MarginR, MarginV, Encoding")


def _dur(path: Path) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)])
    return float(out)


def _run(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def _ass_ts(t: float) -> str:
    h, rem = divmod(max(t, 0.0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _srt_ts(t: float) -> str:
    ms = round(max(t, 0.0) * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap(text: str, width: int) -> list[str]:
    """Split into at most two balanced lines on word boundaries."""
    if len(text) <= width:
        return [text]
    words, best = text.split(), None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = abs(len(a) - len(b))
        if best is None or score < best[0]:
            best = (score, [a, b])
    return best[1] if best else [text]


class BookSceneAssemble(BaseTool):
    name = "book_scene_assemble"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "ffmpeg"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL
    dependencies = ["binary:ffmpeg", "binary:ffprobe"]
    install_instructions = "Install ffmpeg built with libass (apt install ffmpeg fonts-freefont-ttf)"

    capabilities = ["concat_shots", "burn_rtl_captions", "text_overlays", "export_srt", "export_clean_cut"]
    best_for = [
        "cutting a book scene from its book_shot_list and rendered shots",
        "Hebrew/Arabic captions with correct punctuation direction",
        "readable book text (letters, dedications) over generated footage",
        "handing a clean cut + SRT to DaVinci Resolve",
    ]

    input_schema = {
        "type": "object",
        "required": ["project_id"],
        "properties": {
            "project_id": {"type": "string", "description": "projects/<project_id>/ holds book_shot_list.json and assets/shots/<shot_id>.mp4"},
            "name": {"type": "string", "description": "Output base name (default: project_id)"},
            "order": {"type": "array", "items": {"type": "string"}, "description": "Shot ids in cut order (default: shot list order)"},
            "language": {"type": "string", "description": "Caption language code (default: he)"},
            "captions": {"type": "boolean", "description": "Burn captions into the final cut (default true)"},
            "end_card": {"type": "object", "properties": {
                "title": {"type": "string"}, "subtitle": {"type": "string"},
                "seconds": {"type": "number"}}, "description": "Optional closing card text (prefer verbatim book lines)"},
            "font": {"type": "string", "description": "Caption font family (default FreeSerif - has Hebrew/Arabic glyphs)"},
            "resolution": {"type": "string", "description": "WxH, default 1280x720"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=2000, network_required=False)
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = []
    side_effects = ["writes projects/<project_id>/renders/"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if shutil.which("ffmpeg") and shutil.which("ffprobe") else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    # ----------------------------------------------------------------------------------------
    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        try:
            data = self._assemble(inputs)
        except subprocess.CalledProcessError as exc:
            return ToolResult(success=False, error=f"ffmpeg failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")
        return ToolResult(success=True, data=data, artifacts=[data["final"], data["clean"], data["srt"]],
                          duration_seconds=time.time() - start)

    def _rtl(self, text: str, lang: str) -> str:
        return f"{RLE}{text}{RLM}{PDF}" if lang in RTL_LANGS else text

    def _assemble(self, inputs: dict[str, Any]) -> dict[str, Any]:
        pid = inputs["project_id"]
        root = PROJECTS_DIR / pid
        shots_dir, out_dir, work = root / "assets" / "shots", root / "renders", root / "renders" / "_work"
        work.mkdir(parents=True, exist_ok=True)
        sl = json.loads((root / "book_shot_list.json").read_text(encoding="utf-8"))
        validate_artifact("book_shot_list", sl)
        shots = {s["id"]: s for s in sl["shots"]}
        order = inputs.get("order") or [s["id"] for s in sl["shots"]]
        missing = [sid for sid in order if sid not in shots or not (shots_dir / f"{sid}.mp4").exists()]
        if missing:
            raise FileNotFoundError(f"missing shots: {missing}")
        lang = inputs.get("language") or "he"
        font = inputs.get("font") or "FreeSerif"
        w, h = (int(x) for x in (inputs.get("resolution") or "1280x720").split("x"))
        name = inputs.get("name") or pid
        enc = 177 if lang == "he" else 178 if lang == "ar" else 1

        # 1. normalise every shot (and composite readable text where requested)
        parts: list[tuple[str, Path, float]] = []
        for sid in order:
            src = shots_dir / f"{sid}.mp4"
            dst = work / f"{sid}_n.mp4"
            overlays = shots[sid].get("text_overlays") or []
            if overlays:
                self._overlay_shot(src, dst, overlays, lang, font, w, h, enc, work)
            else:
                _run(["-i", str(src), "-vf", f"scale={w}:{h},fps=24,format=yuv420p", "-an",
                      "-c:v", "libx264", "-crf", "18", str(dst)])
            parts.append((sid, dst, _dur(dst)))

        # 2. clean cut (no captions, no end card) + SRT that matches it exactly
        clean = out_dir / f"{name}_clean.mp4"
        self._concat([p for _, p, _ in parts], clean, work)
        srt_lines, t = [], 0.0
        for n, (sid, _, d) in enumerate(parts, start=1):
            text = shots[sid]["source_quote"]
            body = "\n".join(f"{RLM}{line}{RLM}" if lang in RTL_LANGS else line for line in _wrap(text, 42))
            srt_lines.append(f"{n}\n{_srt_ts(t + 0.3)} --> {_srt_ts(t + d - 0.2)}\n{body}\n")
            t += d
        srt = out_dir / f"{name}.{lang}.srt"
        srt.write_text("\n".join(srt_lines), encoding="utf-8-sig")

        # 3. final cut: clean + optional end card + burned captions
        seq = [p for _, p, _ in parts]
        card = inputs.get("end_card")
        card_len = float((card or {}).get("seconds") or 3.5)
        if card:
            card_clip = work / "end_card.mp4"
            _run(["-f", "lavfi", "-i", f"color=c=0xF3E9D2:s={w}x{h}:d={card_len}:r=24",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", str(card_clip)])
            seq.append(card_clip)
        joined = work / "joined.mp4"
        self._concat(seq, joined, work)

        ass = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0", "",
               "[V4+ Styles]", _STYLE_FMT,
               f"Style: Cap,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,3,10,0,2,90,90,36,{enc}",
               f"Style: Card,{font},56,&H00203040,&H00203040,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,5,60,60,0,{enc}",
               f"Style: Card2,{font},40,&H00405060,&H00405060,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,60,60,0,{enc}",
               "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
        t = 0.0
        if inputs.get("captions", True):
            for sid, _, d in parts:
                text = "\\N".join(self._rtl(line, lang) for line in _wrap(shots[sid]["source_quote"], 60))
                ass.append(f"Dialogue: 0,{_ass_ts(t + 0.3)},{_ass_ts(t + d - 0.2)},Cap,,0,0,0,,{text}")
                t += d
        else:
            t = sum(d for _, _, d in parts)
        if card:
            if card.get("title"):
                ass.append(f"Dialogue: 0,{_ass_ts(t + 0.2)},{_ass_ts(t + card_len)},Card,,0,0,0,,"
                           f"{{\\pos({w // 2},{int(h * 0.44)})}}{self._rtl(card['title'], lang)}")
            if card.get("subtitle"):
                ass.append(f"Dialogue: 0,{_ass_ts(t + 0.6)},{_ass_ts(t + card_len)},Card2,,0,0,0,,"
                           f"{{\\pos({w // 2},{int(h * 0.57)})}}{self._rtl(card['subtitle'], lang)}")
        ass_path = work / "captions.ass"
        ass_path.write_text("\n".join(ass) + "\n", encoding="utf-8")
        final = out_dir / f"{name}.mp4"
        _run(["-i", str(joined), "-vf", f"ass={ass_path}", "-c:v", "libx264", "-crf", "18",
              "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(final)])
        return {"final": str(final), "clean": str(clean), "srt": str(srt),
                "duration_seconds": round(_dur(final), 2), "clean_duration_seconds": round(_dur(clean), 2),
                "order": order}

    def _concat(self, clips: list[Path], out: Path, work: Path) -> None:
        lst = work / f"{out.stem}_list.txt"
        lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
        _run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])

    def _overlay_shot(self, src: Path, dst: Path, overlays: list[dict], lang: str, font: str,
                      w: int, h: int, enc: int, work: Path) -> None:
        """Real book text, one overlay after another, over a slowed and softened copy of the shot."""
        per = [float(o.get("seconds") or max(3.0, min(6.0, len(o["text"]) / 18))) for o in overlays]
        total = sum(per) + 0.6
        slow = max(1.0, total / max(_dur(src), 0.1))
        ass = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0", "",
               "[V4+ Styles]", _STYLE_FMT,
               f"Style: Txt,{font},52,&H00101820,&H00101820,&H00D8EAF3,&H00000000,1,0,0,0,100,100,0,0,1,2,0,5,170,170,0,{enc}",
               f"Style: Who,{font},38,&H00304A6A,&H00304A6A,&H00D8EAF3,&H00000000,1,0,0,0,100,100,0,0,1,2,0,5,170,170,0,{enc}",
               "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
        t = 0.3
        for o, d in zip(overlays, per):
            body = "\\N".join(self._rtl(line, lang) for line in _wrap("“" + o["text"] + "”", 48))
            ass.append(f"Dialogue: 0,{_ass_ts(t)},{_ass_ts(t + d - 0.3)},Txt,,0,0,0,,"
                       f"{{\\pos({w // 2},{int(h * 0.35)})\\fad(400,300)}}{body}")
            if o.get("attribution"):
                ass.append(f"Dialogue: 0,{_ass_ts(t + 0.5)},{_ass_ts(t + d - 0.3)},Who,,0,0,0,,"
                           f"{{\\pos({w // 2},{int(h * 0.58)})\\fad(400,300)}}{self._rtl(o['attribution'], lang)}")
            t += d
        ass_path = work / f"{src.stem}_overlay.ass"
        ass_path.write_text("\n".join(ass) + "\n", encoding="utf-8")
        box_y, box_h = int(h * 0.2), int(h * 0.46)
        vf = (f"setpts={slow:.3f}*PTS,scale={w}:{h},fps=24,gblur=sigma=7,eq=brightness=0.06:saturation=0.75,"
              f"drawbox=x={int(w * 0.09)}:y={box_y}:w={int(w * 0.82)}:h={box_h}:color=0xF3E9D2@0.78:t=fill,"
              f"tpad=stop_mode=clone:stop_duration={total},ass={ass_path},trim=duration={total:.2f},format=yuv420p")
        _run(["-i", str(src), "-vf", vf, "-an", "-c:v", "libx264", "-crf", "18", str(dst)])
