"""Verify saved NGT interval boundaries, lossless first frames and figure bundles."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from aquabeacon_analysis.experiments import load_experiment


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def frame_digest(path, offset, journal):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-threads", "2", "-noautorotate"]
    if offset is not None:
        command += ["-ss", f"{offset:.9f}"]
    command += ["-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "framemd5", "-"]
    entry = {"command": command, "started_utc": datetime.now(UTC).isoformat(),
             "purpose": "verify first displayed video frame"}
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace") as process:
        entry["pid"] = process.pid
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        finally:
            entry["exit_code"] = process.returncode
            entry["ended_utc"] = datetime.now(UTC).isoformat()
            journal.append(entry)
        if process.returncode:
            raise ValueError(stderr)
    return next(line.rsplit(",", 1)[-1].strip() for line in stdout.splitlines()
                if line and not line.startswith("#"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    config = json.loads((Path.cwd() / exp.metadata["config_paths"][0]).read_text())
    result = {"experiment_id": args.experiment, "started_utc": datetime.now(UTC).isoformat(),
              "controller_pid": os.getpid(), "csv": [], "videos": [], "figures": [],
              "raw_binary_hashes": [], "processes": []}
    for segment in config["segments"]:
        report = Path("results") / segment["experiment_id"] / "synchronization/manifest.json"
        manifest = json.loads(report.read_text())
        for info in manifest["sensor_files"]:
            count = 0
            with Path(info["output"]).open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    utc = int(row["sync_utc_us"])
                    if not manifest["start_us"] <= utc < manifest["end_us"]:
                        raise AssertionError(f'Outside segment: {info["output"]}')
                    if abs(float(row["experiment_time_s"]) -
                           (utc - manifest["start_us"]) / 1e6) > 1.1e-6:
                        raise AssertionError("Incorrect experiment relative time")
                    count += 1
            if count != info["selected_rows"]:
                raise AssertionError("Subset count mismatch")
            result["csv"].append({"path": info["output"], "rows": count, "all_in_interval": True})
        for info in manifest["videos"]:
            original = frame_digest(info["source"], info["available_source_video_s"][0],
                                    result["processes"])
            clip = frame_digest(info["output"], None, result["processes"])
            match = original == clip
            result["videos"].append({"path": info["output"], "original_frame_md5": original,
                                     "clip_frame_md5": clip, "first_displayed_frame_matches": match,
                                     "display_duration_error_s": info["duration_error_s"]})
            if not match:
                raise AssertionError(f'First displayed frame differs: {info["output"]}')
        print(f'{segment["experiment_id"]}: CSV intervals and both source frames match', flush=True)
    for directory in sorted(Path("results").glob("20261028_ngt_00[0-4]")):
        for png in directory.rglob("*.png"):
            # All PNGs generated in this study are research figures.
            siblings = [png.with_suffix(suffix) for suffix in
                        (".svg", ".figure.pickle", ".plot.json")]
            if not all(path.is_file() for path in siblings):
                raise AssertionError(f"Incomplete editable plot: {png}")
            result["figures"].append({"path": str(png), "editable_bundle_complete": True})
    ingest = json.loads((exp.stage_path("results") / "ingest/decode_manifest.json").read_text())
    for source in ingest["sources"]:
        path = Path(source["source"])
        actual = digest(path)
        if actual != source["source_sha256"]:
            raise AssertionError(f"Raw source changed: {path}")
        result["raw_binary_hashes"].append({"path": str(path), "sha256_unchanged": True})
    result["completed_utc"] = datetime.now(UTC).isoformat()
    result["status"] = "PASS"
    output = exp.stage_path("results") / "verification.json"
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "csv_files": len(result["csv"]),
                      "videos": len(result["videos"]), "editable_figures": len(result["figures"]),
                      "raw_hashes": len(result["raw_binary_hashes"])}))


if __name__ == "__main__":
    main()
