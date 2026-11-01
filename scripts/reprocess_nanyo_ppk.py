"""Replay saved RTKLIB commands against the downloaded GEONET/rover observations."""

import argparse
import json
import subprocess
from pathlib import Path

from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    folder = Path("data/interim") / args.experiment / "gnss"
    records = []
    for run in ["ar_continuous", "float"]:
        command = json.loads((folder / f"{run}.command.json").read_text())
        with (folder / f"{run}.log").open("w") as log:
            process = subprocess.run(command, stdout=log, stderr=log, timeout=180, check=True)
        records.append(
            {
                "run": run,
                "returncode": process.returncode,
                "output_sha256": sha256(folder / f"{run}.pos"),
            }
        )
    (folder / "reprocess.json").write_text(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
