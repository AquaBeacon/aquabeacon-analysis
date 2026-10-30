"""Freeze a nominal CAD rig for future data, without opening any experiment inputs."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from aquabeacon_analysis.cad_rig import (
    build_profile,
    export_assets,
    safe_id,
    sha256,
    verify_sources,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rig-id", default="cad_20261027")
    parser.add_argument("--mechanical-root", type=Path, default=ROOT.parent / "mechanical")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="verify frozen source hashes only")
    mode.add_argument("--bind-experiment", help="explicitly bind a future experiment; no analysis")
    args = parser.parse_args()
    safe_id(args.rig_id)
    profile_path = ROOT / "calibration/rigs" / f"{args.rig_id}.json"
    output = ROOT / "data/interim/cad" / args.rig_id
    if profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        verify_sources(profile, args.mechanical_root, ROOT)
    elif args.verify or args.bind_experiment:
        parser.error(f"create profile first: {profile_path}")
    else:
        profile = build_profile(args.mechanical_root, args.rig_id, ROOT)
    if args.bind_experiment:
        safe_id(args.bind_experiment)
        destination = ROOT / "results" / args.bind_experiment / "cad_geometry"
        if destination.exists():
            parser.error(f"binding already exists: {destination}")
        write_json(destination / "rig.json", profile)
        write_json(destination / "manifest.json", {
            "experiment_id": args.bind_experiment, "rig_id": args.rig_id,
            "profile_sha256": sha256(profile_path), "status": profile["status"],
            "automatic_corrections_applied": False,
        })
        (destination / "report.md").write_text(
            f"# CAD geometry: {args.rig_id}\n\n"
            "Nominal design snapshot only; measured extrinsics remain unavailable.\n"
            "See rig.json and manifest.json. No lever-arm correction was applied.\n",
            encoding="utf-8",
        )
    elif not args.verify:
        assets = export_assets(profile, args.mechanical_root, output)
        verify_sources(profile, args.mechanical_root, ROOT)
        # Do not publish a new profile until both bodies export successfully.
        if not profile_path.exists():
            write_json(profile_path, profile)
        write_json(output / "rig.json", profile)
        write_json(output / "assets.json", {
            "rig_id": args.rig_id, "profile_sha256": sha256(profile_path), "assets": assets,
            "units": "m", "status": profile["status"],
            "generator_sha256": {
                "scripts/prepare_cad_rig.py": sha256(Path(__file__)),
                "src/aquabeacon_analysis/cad_rig.py": sha256(
                    ROOT / "src/aquabeacon_analysis/cad_rig.py"
                ),
            },
        })
        with (output / "sensor_references.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["sensor_id", "body_frame", "reference_kind", "reference_x_m",
                             "reference_y_m", "reference_z_m", "measurement_center", "limitation"])
            for sensor in profile["sensors"]:
                writer.writerow([sensor["id"], sensor["body"] + "_body", sensor["reference_kind"],
                                 *sensor["reference_position_body_m"], "UNKNOWN", sensor["limitation"]])
    print(profile_path)
    print(f"{len(profile['components'])} CAD components; {len(profile['sensors'])} sensor references")


if __name__ == "__main__":
    main()
