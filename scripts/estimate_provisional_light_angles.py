"""Export hypothetical camera-relative angles from previously tracked light CSVs."""

from __future__ import annotations

import argparse
from pathlib import Path

from aquabeacon_analysis.provisional_angles import export_provisional_angles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, default=Path("results"))
    parser.add_argument("--session", action="append", dest="sessions")
    parser.add_argument(
        "--assumption", type=Path, default=Path("calibration/parent_camera_provisional.json")
    )
    args = parser.parse_args()
    for directory in sorted(args.results_root.glob("*/light_detection")):
        if args.sessions and directory.parent.name not in args.sessions:
            continue
        if not (directory / "tracked_light.csv").exists():
            continue
        output, manifest = export_provisional_angles(directory, args.assumption)
        print(output)
        print(manifest)


if __name__ == "__main__":
    main()
