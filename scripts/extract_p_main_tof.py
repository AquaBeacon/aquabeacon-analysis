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

from aquabeacon_analysis.experiments import (
    experiment_context,
    load_experiment,
    validate_experiment_id,
)
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
            repository: Path, *, experiment_id: str | None = None,
            source_suffix: str | None = None, c_main_manifest: Path | None = None) -> list[Path]:
    selected_c_data = None
    if experiment_id is not None:
        validate_experiment_id(experiment_id)
        if source_suffix is None or len(source_suffix) != 5 or not source_suffix.isdigit():
            raise ValueError("register a five-digit p_main_suffix before P_MAIN extraction")
        source = raw_root / f"P_MAIN_HEALTH_RANGE_{source_suffix}.csv"
        if not source.is_file():
            raise FileNotFoundError(source)
        sources = [source]
        if c_main_manifest is not None:
            selected_c_data = json.loads(c_main_manifest.read_text(encoding="utf-8"))
            if selected_c_data["experiment_id"] != experiment_id:
                raise ValueError("C_MAIN preprocessing manifest belongs to a different experiment")
    else:
        sources = sorted(raw_root.glob("P_MAIN_HEALTH_RANGE_*.csv"))
    if not sources:
        raise FileNotFoundError(f"no P_MAIN health logs under {raw_root}")
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository,
                         capture_output=True, text=True, check=False)
    conditions = {
        folder: {str(path.relative_to(repository)): sha256(path)
                 for path in sorted((repository / folder).glob("*")) if path.is_file()}
        for folder in ("config", "calibration")
    }
    manifests = []
    for source in sources:
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
        target_id = experiment_id or f"{date}_pmain_{source.stem[-3:]}"
        if selected_c_data is not None:
            c_start = selected_c_data["window_start_utc_us"]
            c_end = selected_c_data["window_end_utc_us_exclusive"]
            if max(start, c_start) >= min(end, c_end):
                raise ValueError("P_MAIN valid ToF window does not overlap the selected C_MAIN window")
            start, end = c_start, c_end
        output = processed_root / target_id / "tof_window" / "p_main" / source.name
        outputs = [write_window(source, output, rows, start, end)]
        overlaps = []
        overlap_manifests = (sorted(results_root.glob("*_cmain_*/preprocessing/manifest.json"))
                             if experiment_id is None else [])
        for c_manifest in overlap_manifests:
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
            "experiment_id": target_id,
            "source": str(source), "source_sha256": sha256(source),
            "window_start_utc_us": start, "window_end_utc_us_exclusive": end,
            "window_start_utc": datetime.fromtimestamp(start / 1e6, UTC).isoformat(),
            "window_end_utc_exclusive": datetime.fromtimestamp(end / 1e6, UTC).isoformat(),
            "selection": ("selected C_MAIN preprocessing window; require UTC overlap with P_MAIN"
                          if selected_c_data is not None else
                          "first run of at least 5 valid rows through first invalid row after final valid reading"),
            "selected_c_main_manifest": str(c_main_manifest) if c_main_manifest else None,
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
        analysis = "p_main_preprocessing" if experiment_id is not None else "preprocessing"
        report = results_root / target_id / analysis / "manifest.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifests.append(report)
    return manifests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--experiment", help="registered experiment ID")
    scope.add_argument("--all-experiments", action="store_true", help="legacy global UTC matching")
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--processed-root", type=Path)
    parser.add_argument("--results-root", type=Path)
    args = parser.parse_args()
    repository = Path.cwd().resolve()
    options = {}
    context = None
    try:
        if args.experiment:
            if any(value is not None for value in (args.raw_root, args.processed_root, args.results_root)):
                raise ValueError("--experiment cannot be combined with root overrides")
            experiment = load_experiment(repository, args.experiment)
            args.raw_root = experiment.raw_root / "P_MAIN"
            if experiment.raw_root == repository / "data/raw":
                args.raw_root = experiment.raw_root  # Registered legacy flat layout.
            args.processed_root = repository / "data/processed"
            args.results_root = repository / "results"
            options = {"experiment_id": experiment.experiment_id,
                       "source_suffix": experiment.metadata.get("p_main_suffix")}
            if experiment.metadata.get("c_main_suffix") is not None:
                options["c_main_manifest"] = (experiment.stage_path("results") /
                                               "preprocessing/manifest.json")
            context = experiment_context(experiment, "extract-p-main-tof")
        else:
            args.raw_root = args.raw_root or repository / "data/raw"
            args.processed_root = args.processed_root or repository / "data/processed"
            args.results_root = args.results_root or repository / "results"
        for output in (args.processed_root.resolve(), args.results_root.resolve()):
            raw = (repository / "data/raw").resolve()
            if (output.is_relative_to(raw) or raw.is_relative_to(output)
                    or output.is_relative_to(args.raw_root.resolve())
                    or args.raw_root.resolve().is_relative_to(output)):
                raise ValueError(f"output root overlaps raw inputs: {output}")
        manifests = extract(args.raw_root, args.processed_root, args.results_root, repository,
                            **options)
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    for manifest in manifests:
        if context is not None:
            (manifest.parent / "experiment_context.json").write_text(
                json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(manifest)


if __name__ == "__main__":
    main()
