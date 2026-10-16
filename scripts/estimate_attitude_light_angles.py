"""Apply P_MAIN fused attitude to provisional child-light bearing estimates."""

from __future__ import annotations

import argparse
from pathlib import Path

from aquabeacon_analysis.attitude_light_angles import export_attitude_angles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", dest="sessions")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sessions = args.sessions or ["20260922_cmain_022", "20260922_cmain_023", "20260922_cmain_024"]
    for session in sessions:
        output, manifest = export_attitude_angles(session, root)
        print(output)
        print(manifest)


if __name__ == "__main__":
    main()
