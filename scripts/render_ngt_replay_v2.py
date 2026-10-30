"""Bounded single-renderer NGT replay export with exact process ownership and MP4 encoding."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("position_attitude", "map_vehicles", "map_only")


def run_owned(command, *, purpose, output, timeout):
    """Reap only the exact subprocess handle created here, including cancellation/failure."""
    record = {"started_utc": datetime.now(UTC).isoformat(), "purpose": purpose,
              "owner_pid": os.getpid(), "command": command}
    with output.with_suffix(".log").open("w", encoding="utf8") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        record["pid"] = process.pid
        output.with_suffix(".process.json").write_text(json.dumps(record, indent=2), encoding="utf8")
        print(json.dumps(record), flush=True)
        try:
            returncode = process.wait(timeout=timeout)
            if returncode:
                raise RuntimeError(f"{purpose} failed ({returncode}); see {output.with_suffix('.log')}")
        except BaseException as error:
            record["failure"] = str(error)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=15)
            raise
        finally:
            record.update(ended_utc=datetime.now(UTC).isoformat(), exited=process.poll() is not None,
                          returncode=process.poll())
            output.with_suffix(".process.json").write_text(json.dumps(record, indent=2), encoding="utf8")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_004"])
    parser.add_argument("--variant", action="append", choices=VARIANTS)
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--frames", type=int, default=225)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--encode-only", action="store_true")
    parser.add_argument("--clean-existing", action="store_true",
                        help="Render standalone stills from verified blends; preserve existing movies")
    args = parser.parse_args()
    output = ROOT/f"results/{args.experiment}/presentation_v2/render"
    output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic()+1200
    records = []
    for variant in args.variant or VARIANTS:
        folder = output/variant
        folder.mkdir(exist_ok=True)
        if not args.encode_only:
            command = ["C:/Program Files/Blender Foundation/Blender 5.2/blender.exe", "-b", "-t", "2",
                       "--python-exit-code", "1", "--python", str(ROOT/"blender/create_ngt_replay_v2.py"),
                       "--", "--experiment", args.experiment, "--variant", variant,
                       "--frames", str(args.frames), "--fps", str(args.fps)]
            command.append("--clean-existing" if args.clean_existing else
                           "--draft" if args.draft else "--render")
            records.append(run_owned(command, purpose=f"Single owned Eevee {variant} renderer",
                                     output=folder/("clean" if args.clean_existing else
                                                    "draft" if args.draft else "render"),
                                     timeout=max(1, deadline-time.monotonic())))
        if args.clean_existing:
            continue
        if not args.draft:
            record = json.loads((folder/"verification.json").read_text(encoding="utf8"))
            if record["draft"] or len(record["source_indices"]) != args.frames:
                raise ValueError("Verified full-resolution frames are required for encoding")
            frames = list((folder/"frames").glob("*.png"))
            if len(frames) != args.frames:
                raise ValueError("PNG count does not match requested verified output frame count")
            records.append(run_owned([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-threads", "2",
                                      "-framerate", str(args.fps), "-i", str(folder/"frames/%04d.png"),
                                      "-frames:v", str(args.frames), "-c:v", "libx264", "-threads", "2",
                                      "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart",
                                      str(folder/(variant+"_1080.mp4"))],
                                     purpose=f"Owned {variant} MP4 encoder", output=folder/"encode",
                                     timeout=min(120, max(1, deadline-time.monotonic()))))
    if not args.draft and set(args.variant or VARIANTS) >= {"map_vehicles", "map_only"}:
        a, b = [json.loads((output/v/"verification.json").read_text(encoding="utf8"))
                for v in ("map_vehicles", "map_only")]
        assert a["source_indices"] == b["source_indices"]
        assert a["output_times"] == b["output_times"]
    (output/("clean_processes.json" if args.clean_existing else
             "draft_processes.json" if args.draft else "processes.json")).write_text(
        json.dumps({"records": records, "all_owned_processes_exited": all(r["exited"] for r in records)},
                   indent=2), encoding="utf8")
    print("Completed; all task-owned renderer and encoder processes exited.", flush=True)


if __name__ == "__main__":
    main()
