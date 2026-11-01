import argparse
import csv
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Group acoustic candidates for selected Nanyo intervals."
)
parser.add_argument(
    "--experiment",
    action="append",
    required=True,
    choices=["20261031_nanyo_001", "20261031_nanyo_002"],
)
args = parser.parse_args()
for eid in args.experiment:
    p = Path("data/processed") / eid / "synchronized/child/C_MAIN_HEALTH_RANGE_35545.shot.csv"
    rows = list(csv.DictReader(p.open(encoding="utf-8")))
    groups = {}
    for r in rows:
        key = tuple(
            r[k]
            for k in [
                "frequency_hz",
                "acoustic_pattern_id",
                "pattern_gap1_us",
                "pattern_gap2_us",
                "ranging_rate_hz",
            ]
        )
        g = groups.setdefault(key, {"shots": 0, "candidates": 0})
        g["shots"] += 1
        g["candidates"] += int(r["range_state"] == "3" and float(r["tof_us"]) > 0)
    out = []
    for key, g in groups.items():
        out.append(
            dict(
                zip(
                    [
                        "frequency_hz",
                        "acoustic_pattern_id",
                        "gap1_us",
                        "gap2_us",
                        "ranging_rate_hz",
                    ],
                    key,
                )
            )
            | g
            | {"candidate_fraction": g["candidates"] / g["shots"]}
        )
    p = Path("results") / eid / "evaluation/acoustic_pattern_groups.csv"
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(eid, len(out))
