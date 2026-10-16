"""Extract timestamped P_MAIN health rows from sustained ToF windows."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from aquabeacon_analysis.tof_preprocess import tof_window


def utc_us(row: dict[str, str]) -> int:
    """Map a P_MAIN monotonic timestamp through its recorded UTC anchor."""
    mono = int(row["mono_us"])
    if mono > int(row["utc_valid_until_us"]):
        raise ValueError("P_MAIN UTC anchor expired")
    return int(row["utc_anchor_epoch_us"]) + mono - int(row["utc_anchor_mono_us"])


def valid_tof(row: dict[str, str]) -> bool:
    return int(row["tof_us"]) > 0 and row["range_state"] in {"2", "3"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_window(source: Path, destination: Path, rows: list[dict[str, str]],
                 start: int, end: int) -> dict:
    selected = [row for row in rows if start <= int(row["sync_utc_us"]) < end]
    fields = ["window_time_s", "tof_valid", "sync_utc_us", "sync_utc_iso",
              *[key for key in rows[0] if key != "sync_utc_us"]]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in selected:
            timestamp = int(row["sync_utc_us"])
            writer.writerow({"window_time_s": f"{(timestamp - start) / 1e6:.6f}",
                             "tof_valid": int(valid_tof(row)),
                             "sync_utc_iso": datetime.fromtimestamp(timestamp / 1e6, UTC).isoformat(),
                             **row})
    return {"output": str(destination), "rows": len(selected),
            "valid_tof_rows": sum(valid_tof(row) for row in selected)}


def extract(raw_root: Path, processed_root: Path, results_root: Path,
            repository: Path) -> list[Path]:
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository,
                         capture_output=True, text=True, check=False)
    conditions = {
        folder: {str(path.relative_to(repository)): sha256(path)
                 for path in sorted((repository / folder).glob("*")) if path.is_file()}
        for folder in ("config", "calibration")
    }
    manifests = []
    for source in sorted(raw_root.glob("P_MAIN_HEALTH_RANGE_*.csv")):
        with source.open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        if not rows:
            raise ValueError(f"empty P_MAIN CSV: {source}")
        for line_number, row in enumerate(rows, start=2):
            row["source_csv_line"] = str(line_number)
            row["sync_utc_us"] = str(utc_us(row))
        backward_steps = [int(a["sync_utc_us"]) - int(b["sync_utc_us"])
                          for a, b in pairwise(rows)
                          if int(a["sync_utc_us"]) > int(b["sync_utc_us"])]
        rows.sort(key=lambda row: int(row["sync_utc_us"]))
        start, end = tof_window(rows)
        date = datetime.fromtimestamp(start / 1e6, UTC).strftime("%Y%m%d")
        experiment_id = f"{date}_pmain_{source.stem[-3:]}"
        output = processed_root / experiment_id / "tof_window" / "p_main" / source.name
        outputs = [write_window(source, output, rows, start, end)]
        overlaps = []
        for c_manifest in sorted(results_root.glob("*_cmain_*/preprocessing/manifest.json")):
            c_data = json.loads(c_manifest.read_text(encoding="utf-8"))
            c_start = c_data["window_start_utc_us"]
            c_end = c_data["window_end_utc_us_exclusive"]
            if max(start, c_start) >= min(end, c_end):
                continue
            c_output = (processed_root / c_data["experiment_id"] / "tof_window"
                        / "p_main" / source.name)
            record = write_window(source, c_output, rows, c_start, c_end)
            outputs.append(record)
            overlaps.append({"experiment_id": c_data["experiment_id"],
                             "c_main_manifest": str(c_manifest), **record})
        manifest = {
            "experiment_id": experiment_id,
            "source": str(source), "source_sha256": sha256(source),
            "window_start_utc_us": start, "window_end_utc_us_exclusive": end,
            "window_start_utc": datetime.fromtimestamp(start / 1e6, UTC).isoformat(),
            "window_end_utc_exclusive": datetime.fromtimestamp(end / 1e6, UTC).isoformat(),
            "selection": "first run of at least 5 valid rows through first invalid row after final valid reading",
            "valid_tof_condition": "tof_us > 0 and range_state in {2,3}",
            "timestamp_formula": "sync_utc_us = utc_anchor_epoch_us + mono_us - utc_anchor_mono_us; require mono_us <= utc_valid_until_us",
            "timestamp_order": "stable UTC sort; source_csv_line retains original order",
            "backward_clock_steps": len(backward_steps),
            "maximum_backward_clock_step_us": max(backward_steps, default=0),
            "invalid_rows": "retained within selected window with tof_valid=0",
            "git_commit": git.stdout.strip() if git.returncode == 0 else None,
            "input_conditions_sha256": conditions,
            "outputs": outputs, "c_main_overlaps": overlaps,
        }
        report = results_root / experiment_id / "preprocessing" / "manifest.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifests.append(report)
    return manifests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    parser.add_argument("--results-root", type=Path, default=Path("results"))
    args = parser.parse_args()
    for manifest in extract(args.raw_root, args.processed_root, args.results_root,
                            Path.cwd().resolve()):
        print(manifest)


if __name__ == "__main__":
    main()
