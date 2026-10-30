"""Extract original camera telemetry for the explicitly selected NGT acquisition."""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.ngt_sync import extract_telemetry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    config = json.loads((Path.cwd() / exp.metadata["config_paths"][0]).read_text())
    print(json.dumps({"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
                      "purpose": "DJI telemetry extraction", "owner": args.experiment}), flush=True)
    for role in ("parent", "child"):
        video = exp.raw_root / config[f"{role}_video"]
        source = video.with_suffix(".LRF")
        output = exp.stage_path("interim") / "cameras" / f"{role}_telemetry.csv"
        if output.exists() and output.with_suffix(".json").exists():
            print(f"Reuse {output}", flush=True)
        else:
            print(json.dumps(extract_telemetry(source, output)), flush=True)


if __name__ == "__main__":
    main()
