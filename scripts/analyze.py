"""CLI: analyse one video with explicit provider settings. Used for latency/quality benchmarking.

    python scripts/analyze.py data/nas-test/clip.mp4 --thinking low --resolution low --fps 0.5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.api.config import get_settings  # noqa: E402
from services.ai_gateway.gemini import GeminiProvider  # noqa: E402
from services.block_engine.engine import BlockEngine  # noqa: E402
from services.block_engine.sources import from_path, from_url  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="video path (under NAS_ALLOWED_ROOTS) or YouTube URL")
    ap.add_argument("--model")
    ap.add_argument("--thinking", choices=["low", "medium", "high"])
    ap.add_argument("--resolution", choices=["low", "medium", "high", "ultra_high"])
    ap.add_argument("--fps", type=float)
    ap.add_argument("--prompt", help="prompt version, e.g. v1 or v2")
    ap.add_argument("--tag", default="bench")
    a = ap.parse_args()

    s = get_settings()
    s.ensure_dirs()
    provider = GeminiProvider(
        s.gemini_api_key,
        a.model or s.gemini_model,
        thinking_level=a.thinking or s.gemini_thinking_level,
        video_resolution=a.resolution or s.gemini_video_resolution,
        video_fps=a.fps if a.fps is not None else s.gemini_video_fps,
        delete_uploads=s.gemini_delete_uploads,
        store=s.gemini_store,
        timeout_seconds=s.gemini_timeout_seconds,
    )
    engine = BlockEngine(provider, prompt_version=a.prompt or s.prompt_version, institution_context=s.institution_context, known_people_file=s.known_people_file, people_pass_version=s.people_pass_version or None)
    source = from_url(a.source) if a.source.startswith("http") else from_path(a.source, s.allowed_roots, 0)

    t0 = time.monotonic()
    marks: list[str] = []

    def on_stage(state: str, detail: dict) -> None:
        marks.append(f"{state}@{time.monotonic() - t0:.0f}s")

    label = f"{a.tag}_{engine.prompt_version}_{provider.thinking_level}_{provider.video_resolution}_fps{provider.video_fps or 'default'}"
    try:
        result = engine.analyze(label, source, on_stage)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"label": label, "error": f"{type(exc).__name__}: {exc}", "stages": marks}))
        sys.exit(1)
    out = s.analyses_dir / f"{label}.json"
    out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps({
        "label": label,
        "seconds": result.processing_seconds,
        "stages": marks,
        "usage": result.usage.model_dump(),
        "counts": result.block_counts,
        "repaired": result.repaired,
        "warnings": result.warnings,
        "title": result.analysis.video.title,
        "people": [p.name for p in result.analysis.people],
        "saved": str(out),
    }))


if __name__ == "__main__":
    main()
