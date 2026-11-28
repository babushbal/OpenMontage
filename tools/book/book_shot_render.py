"""Render the shots of a book_shot_list with Kling O3, identities bound from the character bible.

- Characters -> reference_to_video with `elements` from the locked bible (order = @Element1..N).
- `continuity_from_previous` -> the previous clip's last frame becomes start_image_url.
- Shots without characters or a start frame -> text_to_video.
- Existing projects/<id>/assets/shots/<shot>.mp4 are skipped, so a run can be resumed;
  delete a file to re-render that shot.
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
from typing import Any

from lib.paths import PROJECTS_DIR
from schemas.artifacts import validate_artifact
from tools.base_tool import (
    BaseTool, Determinism, ExecutionMode, ResourceProfile, RetryPolicy,
    ToolResult, ToolRuntime, ToolStability, ToolStatus, ToolTier,
)


class BookShotRender(BaseTool):
    name = "book_shot_render"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "video_generation"
    provider = "fal.ai"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API
    dependencies = ["env:FAL_KEY", "binary:ffmpeg"]
    install_instructions = "Configure FAL_KEY (https://fal.ai/dashboard/keys); install ffmpeg."

    capabilities = ["render_book_shots", "identity_locked_video", "shot_continuity"]
    best_for = ["rendering a whole approved book_shot_list with locked character identities"]
    agent_skills = []

    input_schema = {
        "type": "object",
        "required": ["project_id", "book_id"],
        "properties": {
            "project_id": {"type": "string"},
            "book_id": {"type": "string"},
            "shot_ids": {"type": "array", "items": {"type": "string"}, "description": "Only these shots (e.g. ['s1'] for the sample gate)"},
            "model": {"type": "string", "description": "kling-o3-standard (default) or kling-o3-pro"},
            "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"]},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=1000, network_required=True)
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])  # paid submissions are never auto-retried
    idempotency_key_fields = []
    side_effects = ["paid fal.ai video generation", "writes projects/<project_id>/assets/shots/"]

    def get_status(self) -> ToolStatus:
        from tools.video.kling_o3_fal_video import KlingO3FalVideo
        return KlingO3FalVideo().get_status()

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        from tools.video.kling_o3_fal_video import KlingO3FalVideo
        return KlingO3FalVideo().estimate_cost(inputs)  # raises PriceQuoteRequired: quote per-second price

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        from tools.book.character_bible import CharacterBible
        from tools.video.kling_o3_fal_video import KlingO3FalVideo

        start = time.time()
        root = PROJECTS_DIR / inputs["project_id"]
        shots_dir = root / "assets" / "shots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        sl = json.loads((root / "book_shot_list.json").read_text(encoding="utf-8"))
        validate_artifact("book_shot_list", sl)
        cb, kling = CharacterBible(), KlingO3FalVideo()
        gate = cb.execute({"operation": "check_shot_list", "book_id": inputs["book_id"], "book_shot_list": sl})
        if not gate.success or not gate.data.get("passed"):
            return ToolResult(success=False, error=f"shot list failed the fidelity gate: {gate.error or gate.data.get('issues')}")

        wanted = set(inputs.get("shot_ids") or [s["id"] for s in sl["shots"]])
        results, prev, cost = [], None, 0.0
        for shot in sl["shots"]:
            out = shots_dir / f"{shot['id']}.mp4"
            if shot["id"] not in wanted or out.exists():
                if out.exists():
                    prev = out
                    results.append({"shot": shot["id"], "status": "exists"})
                continue
            req: dict[str, Any] = {
                "model": inputs.get("model") or "kling-o3-standard",
                "generation_mode": "reference_to_video",
                "prompt": shot["video_prompt"],
                "duration": str(shot["duration_seconds"]),
                "aspect_ratio": inputs.get("aspect_ratio") or "16:9",
                "generate_audio": False,
                "output_path": str(out),
                "job_path": str(shots_dir / f"{shot['id']}.job.json"),
            }
            if shot["character_ids"]:
                el = cb.execute({"operation": "kling_elements", "book_id": inputs["book_id"],
                                 "character_ids": shot["character_ids"]})
                if not el.success:
                    return ToolResult(success=False, error=f"{shot['id']}: {el.error}", data={"results": results})
                req["elements"] = el.data["elements"]
            if shot.get("continuity_from_previous") and prev is not None:
                frame = shots_dir / f"{shot['id']}_start.png"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.1", "-i", str(prev),
                                "-frames:v", "1", str(frame)], check=True)
                req["start_image_url"] = "data:image/png;base64," + base64.b64encode(frame.read_bytes()).decode()
            if not shot["character_ids"] and "start_image_url" not in req:
                req["generation_mode"] = "text_to_video"
            res = kling.execute(req)
            results.append({"shot": shot["id"], "status": "rendered" if res.success else "failed", "error": res.error})
            if not res.success:
                return ToolResult(success=False, error=f"{shot['id']}: {res.error}", data={"results": results},
                                  duration_seconds=time.time() - start)
            cost += res.cost_usd or 0.0
            prev = out
        return ToolResult(success=True, data={"results": results}, cost_usd=cost,
                          artifacts=[str(shots_dir / f"{r['shot']}.mp4") for r in results],
                          duration_seconds=time.time() - start)
