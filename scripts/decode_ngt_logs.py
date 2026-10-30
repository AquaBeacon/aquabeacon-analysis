"""Export explicitly selected firmware logs without changing recorded data."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_binary import export_binary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--firmware-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--allow-truncated-tail", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    experiment = load_experiment(root, args.experiment)
    output = experiment.stage_path("interim") / "decoded"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    for source in args.source:
        source = source.resolve()
        if not source.is_file() or not source.is_relative_to(experiment.raw_root):
            parser.error("Each source must be a file inside the registered experiment raw_root")
        report = export_binary(source, output / source.parent.name, args.firmware_root.resolve(),
                               args.allow_truncated_tail)
        print(json.dumps({"source": str(source), "git_commit": commit,
                          "streams": {k: v["rows"] for k, v in report["streams"].items()},
                          "complete": report["complete"]}))


if __name__ == "__main__":
    main()
