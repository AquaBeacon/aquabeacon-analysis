"""Audit an explicitly registered child capture; never publish an unverified rotation."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from aquabeacon_analysis.experiments import load_experiment, repository_path
from aquabeacon_analysis.ngt_binary import load_reader, sha256
from aquabeacon_analysis.ngt_sync import extract_telemetry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--firmware-root", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    exp = load_experiment(root, args.experiment)
    config_path = repository_path(root, exp.metadata["config_paths"][0])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    sources = {k: repository_path(root, v) for k, v in config["sources"].items()}
    if any(not p.is_relative_to(exp.raw_root) for p in sources.values()):
        raise ValueError("Calibration inputs must be inside registered raw root")
    output = exp.stage_path("results") / "axis_calibration"
    output.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
                      "owner": args.experiment, "purpose": "child calibration input audit"}),
          flush=True)
    reader = load_reader(args.firmware_root, "binary_log")
    with sources["capture"].open("rb") as handle:
        header = reader.read_header(handle)
        events = [row for record in reader.records(handle) if record["kind"] == 13
                  for row in reader.rows(record)]
    if not events:
        raise ValueError("No body markers")
    missing = []
    for key in ("log_file", "raw_file", "health_file"):
        names = sorted({e[key] for e in events})
        for name in names:
            if Path(name).name != name:
                raise ValueError("Invalid marker source filename")
            path = sources["capture"].parent / name
            if not path.exists():
                missing.append(path.relative_to(root).as_posix())
    phases = []
    for phase in range(1, 6):
        begin = [e for e in events if e["event"] == 2 and e["phase"] == phase]
        end = [e for e in events if e["event"] == 3 and e["phase"] == phase]
        if len(begin) == len(end) == 1:
            phases.append({"phase": phase,
                           "start_mono_s": begin[0]["marker_mono_us"] / 1e6,
                           "end_mono_s": end[0]["marker_mono_us"] / 1e6,
                           "drop_counter_delta": end[0]["drops"] - begin[0]["drops"]})
    telemetry = extract_telemetry(sources["camera_proxy"],
                                 exp.stage_path("interim") / "cameras/calibration_telemetry.csv")
    manifest = {
        "experiment_id": args.experiment, "status": "input_audit_only",
        "rotation_published_by_audit": False, "header": header, "events": events, "phases": phases,
        "missing_marker_sources": missing, "camera_telemetry": telemetry,
        "camera_time_transform": None,
        "source_provenance": {k: {"path": p.relative_to(root).as_posix(),
                                  "sha256": sha256(p)} for k, p in sources.items()},
        "config_sha256": sha256(config_path),
        "reader_sha256": sha256(args.firmware_root / "scripts/binary_log.py"),
        "script_sha256": sha256(Path(__file__)),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                               text=True).strip(),
        "worktree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"],
                                                       cwd=root, text=True)),
        "limitations": ["Marker completion is not numerical calibration acceptance.",
                        "Event 7/mask 15 needs an authoritative axes-only schema review.",
                        "Changed drop counters require explicit missing-sample review.",
                        "No calibration clock offset transfers to another camera clip.",
                        "Camera/light locations are swapped per user; CAD is design-only."],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"missing": missing, "phases": phases, "telemetry": telemetry}), flush=True)


if __name__ == "__main__":
    main()
