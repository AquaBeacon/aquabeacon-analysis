"""Recover exact input-frame timestamps of the saved fps=1 COLMAP images."""

import argparse
import json
import re
import subprocess
from pathlib import Path

from analyze_nanyo import write

from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.time_sync import resolve_ffmpeg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--reuse-log", action="store_true")
    args = parser.parse_args()
    eid = args.experiment
    work = Path(f"data/interim/{eid}/visual_mapping")
    out = Path(f"results/{eid}/visual_mapping")
    m = json.loads((out / "manifest.json").read_text())
    start, end = m["source_interval_s"]
    clock = next(
        v
        for k, v in json.loads(
            Path("results/20261031_nanyo_000/synchronization/manifest.json").read_text()
        )["cameras"].items()
        if k.startswith("child")
    )
    # Same fps filter and trim as map_nanyo.py. A FIFO follows ffmpeg's own
    # read/drop/write trace; no assumption of uniform native frame rate.
    command = [
        str(resolve_ffmpeg()),
        "-v",
        "debug",
        "-threads",
        "2",
        "-ss",
        str(start),
        "-i",
        m["source"],
        "-t",
        str(end - start),
        "-vf",
        "fps=1,scale=960:540",
        "-f",
        "null",
        "-",
    ]
    log = work / "fps_timing_debug.log"
    if not args.reuse_log:
        with log.open("w", encoding="utf-8") as f:
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=f, check=True, timeout=300)
    queue, records = [], []
    # LRF stream time base is independently extracted with ffmpeg's showinfo.
    probe = subprocess.run(
        [
            str(resolve_ffmpeg()),
            "-v",
            "info",
            "-ss",
            str(start),
            "-i",
            m["source"],
            "-frames:v",
            "1",
            "-vf",
            "showinfo",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    num, den = map(int, re.search(r"config in time_base: (\d+)/(\d+)", probe).groups())
    for line in log.read_text(encoding="utf-8").splitlines():
        if "Parsed_fps_" not in line:
            continue
        match = re.search(r"Read frame with in pts (-?\d+), out pts (-?\d+)", line)
        if match:
            queue.append((int(match[1]), int(match[2])))
        elif "Dropping frame with pts" in line:
            queue.pop(0)
        elif "Writing frame with pts" in line:
            match = re.search(r"Writing frame with pts (-?\d+) to pts (-?\d+)", line)
            assert queue[0][1] == int(match[1])
            j = int(match[2])
            name = f"{j:06d}.jpg"
            if (work / "images" / name).exists():
                source_s = start + queue[0][0] * num / den
                records.append(
                    {
                        "image": name,
                        "source_video_s": source_s,
                        "sync_utc_s": clock["utc_at_video_zero_s"] + clock["time_scale"] * source_s,
                        "input_pts": queue[0][0],
                        "input_time_base": f"{num}/{den}",
                    }
                )
            queue.pop(0)
    assert len(records) == m["sampled_frames"]
    write(out / "frame_times.csv", records)
    (out / "frame_times_manifest.json").write_text(
        json.dumps(
            {
                "method": "Replay original ffmpeg fps filter FIFO debug trace",
                "command": command,
                "rows": len(records),
                "code_sha256": sha256(Path(__file__)),
                "log_sha256": sha256(log),
                "precision_s": num / den,
                "source_sha256": sha256(Path(m["source"])),
            },
            indent=2,
        )
    )
    print(eid, len(records), records[0], flush=True)


if __name__ == "__main__":
    main()
