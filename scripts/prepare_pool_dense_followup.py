"""Prepare an explicitly bounded denser pool wall attempt from saved old/new positions."""
import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from analyze_pool_optical_followup import prepare_mapping
from evaluate_position_fallback import dump
from improve_nanyo_light import ROOT, read, relative
from improve_ngt_light import folder
from position_fallback_ngt import load_ngt, numbers

from aquabeacon_analysis.ngt_binary import sha256

CONFIG = ROOT/"config/20261103_field_review_000/pool_mapping_dense.json"


def main():
    parser = argparse.ArgumentParser(__doc__); parser.add_argument("--experiment", required=True)
    args = parser.parse_args(); cfg = json.loads(CONFIG.read_text()); eid = args.experiment
    if eid not in cfg["experiments"]:
        parser.error("Explicit configured pool trial required")
    out = folder(eid, cfg); out.mkdir(parents=True, exist_ok=False)
    data = load_ngt(eid)
    data["metadata"]["parent_video_interval_s"] = cfg["mapping_interval_s"]
    source = ROOT/f"results/{eid}/optical_followup/{cfg['source_position_run']}"
    inputs = [CONFIG, Path(__file__), source/"analysis_manifest.json",
              ROOT/"scripts/analyze_pool_optical_followup.py"]
    results = {}
    for label in ("old", "new"):
        path = source/f"{label}_positions.csv"; inputs.append(path)
        rows = read(path)
        np.testing.assert_array_equal(numbers(rows, ["utc_s"])[:, 0], data["times"])
        results[label] = {"selected": numbers(rows, [f"selected_{a}_m" for a in ("east", "north", "up")])}
    cv2.setNumThreads(1)
    print("dense pool preparation PID", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    inputs += prepare_mapping(eid, cfg, out, data, results)
    dump(out/"analysis_manifest.json", {"experiment_id": eid, "run": cfg["run"],
          "created_utc": datetime.now(UTC).isoformat(), "pid": os.getpid(),
          "inputs_sha256": {relative(p): sha256(p) for p in inputs}, "scope": cfg["scope"], "accuracy_validated": False})


if __name__ == "__main__":
    main()
