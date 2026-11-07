"""Stream transferred envelope CSVs; retain partial captures and acquisition loss."""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import subprocess
from pathlib import Path

import numpy as np

from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.multipath import envelope_events
from aquabeacon_analysis.ngt_binary import sha256


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        fields = list(dict.fromkeys(k for r in rows for k in r))
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    a = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    totals = []
    for eid in a.experiment:
        exp = load_experiment(root, eid)
        folder = exp.stage_path("processed") / "synchronized/child"
        begin_path, = folder.glob("*MULTIPATH*.capture_begin.csv")
        end_path, = folder.glob("*MULTIPATH*.capture_end.csv")
        envelope_path, = folder.glob("*MULTIPATH*.envelope.csv")
        begins = {r["shot_seq"]: r for r in read(begin_path)}
        ends = {r["shot_seq"]: r for r in read(end_path)}
        rows, seen = [], set()
        with envelope_path.open(encoding="utf-8-sig", newline="") as handle:
            for shot, stream in itertools.groupby(csv.DictReader(handle), key=lambda r: r["shot_seq"]):
                if shot in seen:
                    raise ValueError("Noncontiguous shot rows; review order before grouping")
                seen.add(shot)
                samples = [(int(r["sample_index"]), int(r["mono_us"]), float(r["env_adc"]),
                            r["adc_error"] == "True") for r in stream]
                values = np.asarray(samples)
                index, clock, adc, error = values.T
                begin, end = begins.get(shot), ends.get(shot)
                complete = bool(begin and end and len(samples) == int(end["written_samples"]))
                dt = np.diff(clock)
                ordered = bool((dt > 0).all() and (np.diff(index) > 0).all())
                row = {"experiment_id": eid, "shot_seq": shot, "csv_samples": len(samples),
                       "begin_in_segment": bool(begin), "end_in_segment": bool(end),
                       "full_recorded_capture_in_segment": complete,
                       "acquisition_written_samples": int(end["written_samples"]) if end else "",
                       "acquisition_lost_samples": int(end["lost_samples"]) if end else "",
                       "adc_errors": int(error.sum()), "strict_sample_order": ordered,
                       "index_gaps": int(np.maximum(np.diff(index)-1, 0).sum()),
                       "maximum_sample_gap_us": int(dt.max()) if len(dt) else "",
                       "sample_span_s": float((clock[-1]-clock[0])/1e6)}
                # Do not estimate the tail baseline from a capture cut by trial boundaries.
                if complete and ordered and not error.any():
                    t = (clock-clock[0])/1e6
                    events = envelope_events(t, adc)
                    row.update(baseline_adc=events["baseline_adc"],
                               threshold_adc=events["threshold_adc"],
                               events=len(events["events"]),
                               late_events_after_20ms=sum(e["start_s"] >= 0.02 for e in events["events"]))
                rows.append(row)
        out = exp.stage_path("results") / "acoustic_audit_20261103"
        out.mkdir(parents=True, exist_ok=True)
        write(out / "captures.csv", rows)
        summary = {"experiment_id": eid, "begin_records": len(begins), "end_records": len(ends),
                   "completed_reason0": sum(r["reason"] == "0" for r in ends.values()),
                   "no_arrival_reason2": sum(r["reason"] == "2" for r in ends.values()),
                   "other_end_reasons": sorted({r["reason"] for r in ends.values()}-{"0", "2"}),
                   "recorded_captures_in_csv": len(rows), "envelope_rows": sum(r["csv_samples"] for r in rows),
                   "fully_contained_captures": sum(r["full_recorded_capture_in_segment"] for r in rows),
                   "partial_or_boundary_captures": sum(not r["full_recorded_capture_in_segment"] for r in rows),
                   "recorded_lost_samples": sum(int(r["lost_samples"]) for r in ends.values()),
                   "captures_reporting_loss": sum(int(r["lost_samples"]) > 0 for r in ends.values()),
                   "observed_index_gaps": sum(r["index_gaps"] for r in rows),
                   "adc_errors": sum(r["adc_errors"] for r in rows),
                   "all_samples_ordered": all(r["strict_sample_order"] for r in rows)}
        manifest = {**summary,
                    "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                    "calibration": "None; uncalibrated ADC counts, no sound-pressure conversion",
                    "inputs_sha256": {str(p.relative_to(root)): sha256(p)
                    for p in (begin_path, end_path, envelope_path, Path(__file__),
                              root / "src/aquabeacon_analysis/multipath.py")},
                    "interpretation": "Recorded envelope acquisition quality; not direct-path success, acoustic energy, or ground-truth distance error",
                    "partial_policy": "Boundary capture counts retained; no tail-baseline calculation",
                    "event_time_reference": "first retained ADC sample; excludes partial captures; not transmit time",
                    "lost_sample_policy": "No interpolation; acquisition counters and observed index gaps reported separately"}
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        totals.append(summary)
        print(json.dumps(summary), flush=True)
    target = root / "results/20261103_field_review_000/research_review"
    target.mkdir(parents=True, exist_ok=True)
    write(target / "acoustic_capture_quality.csv", totals)


if __name__ == "__main__":
    main()
