"""Create lightweight native-orientation previews on each segment's UTC clock."""

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.time_sync import resolve_ffmpeg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261031_nanyo_000"])
    acq = parser.parse_args().experiment
    output = Path(f"results/{acq}/navigation_replay/media")
    output.mkdir(parents=True, exist_ok=True)
    sync_path = Path(f"results/{acq}/synchronization/manifest.json")
    cameras = json.loads(sync_path.read_text())["cameras"]
    joined = json.loads(Path(f"results/{acq}/segmentation/manifest.json").read_text())[
        "parent_join_mapping"
    ]
    jobs = []
    for suffix in ["001", "002"]:
        eid = f"20261031_nanyo_{suffix}"
        interval = json.loads(Path(f"experiments/{eid}/experiment.json").read_text())[
            "utc_interval_s"
        ]
        for role in ["parent", "child"]:
            for key, clock in cameras.items():
                if not key.startswith(role):
                    continue
                source = Path(clock["source_video"]["path"]).with_suffix(".LRF")
                utc0 = clock["utc_at_video_zero_s"]
                scale = clock["time_scale"]
                end = interval[1]
                if role == "parent":
                    part = next(p for p in joined if p["camera_key"] == key)
                    end = min(end, utc0 + (part["joined_end_s"] - part["joined_start_s"]) * scale)
                start = max(interval[0], utc0)
                if end <= start:
                    continue
                target = output / f"{suffix}_{role}_{source.stem}.mp4"
                command = [
                    str(resolve_ffmpeg()),
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-y",
                    "-threads",
                    "2",
                    "-ss",
                    str((start - utc0) / scale),
                    "-t",
                    str((end - start) / scale),
                    "-noautorotate",
                    "-i",
                    str(source),
                    "-an",
                    "-vf",
                    f"setpts=(PTS-STARTPTS)*{scale:.12f},fps=15,scale=640:360",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "2",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "25",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(target),
                ]
                jobs.append(
                    {
                        "experiment_id": eid,
                        "role": role,
                        "start_s": start - interval[0],
                        "end_s": end - interval[0],
                        "start_utc_s": start,
                        "end_utc_s": end,
                        "source": str(source),
                        "source_start_s": (start - utc0) / scale,
                        "time_scale": scale,
                        "path": "media/" + target.name,
                        "target": str(target),
                        "command": command,
                    }
                )

    def encode(job):
        target = Path(job["target"])
        if not target.exists():
            with target.with_suffix(".log").open("w") as log:
                subprocess.run(job["command"], stdout=log, stderr=log, check=True)
        print("MEDIA", target.name, target.stat().st_size, flush=True)
        return {**job, "sha256": sha256(target), "bytes": target.stat().st_size}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(encode, jobs))
    manifest = {
        "experiment_id": acq,
        "fps": 15,
        "size": [640, 360],
        "audio": False,
        "timing_note": "UTC-scaled preview, resampled at 15 fps. Frame rounding up to one preview frame; native analytical timestamps remain separate. Parent parts retain their piecewise clock.",
        "videos": results,
        "sync_manifest_sha256": sha256(sync_path),
        "code_sha256": sha256(Path(__file__)),
    }
    (output.parent / "media_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
