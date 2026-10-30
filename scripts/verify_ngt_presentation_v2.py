"""Verify saved replay hashes, movie decoding, source preservation and package links."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("position_attitude", "map_vehicles", "map_only")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_004"])
    parser.add_argument("--inputs-only", action="store_true")
    args = parser.parse_args()
    cv2.setNumThreads(2)
    folder = ROOT / f"results/{args.experiment}/presentation_v2"
    manifest = load(folder / "data/manifest.json")
    failures = []
    for group, paths in (("inputs", manifest["inputs_sha256"]),
                         ("outputs", manifest["outputs_sha256"])):
        for relative, expected in paths.items():
            path = (ROOT if group == "inputs" else folder / "data") / relative
            if not path.is_file() or sha(path) != expected:
                failures.append(f"{group} hash mismatch: {relative}")
    baseline = load(ROOT / "results/20261028_ngt_000/presentation/integrity_before.json")
    for record in baseline["raw"]:
        path = ROOT / record["path"]
        stat = path.stat() if path.is_file() else None
        if stat is None or stat.st_size != record["size"] or stat.st_mtime_ns != record["mtime_ns"]:
            failures.append(f"Raw metadata changed since {baseline['captured_utc']}: {record['path']}")
    result = {
        "checked_utc": datetime.now(UTC).isoformat(), "pid": os.getpid(),
        "experiment_id": args.experiment,
        "inputs_sha256_checked": len(manifest["inputs_sha256"]),
        "data_outputs_sha256_checked": len(manifest["outputs_sha256"]),
        "raw_metadata_checked": len(baseline["raw"]),
        "raw_baseline_utc": baseline["captured_utc"],
        "raw_check_scope": "Size and mtime_ns only; full raw content not rehashed",
        "browser_ui": {
            "research_editor": "Verified separately through its standard loopback application, using a QA master copy",
            "research_editor_record": "results/20261028_ngt_001/presentation_figures/editor_validation/verification.json",
            "file_based_hub": "Not browser-tested; earlier local-file protocol request was rejected; no proxy used",
        },
    }
    if not args.inputs_only:
        rendered = load(folder / "render/manifest.json")
        for item in rendered["inputs"] + rendered["outputs"]:
            if sha(ROOT / item["path"]) != item["sha256"]:
                failures.append("Rendered input/output hash mismatch: " + item["path"])
        result["render_input_and_output_hashes_checked"] = len(rendered["inputs"]) + len(rendered["outputs"])
        assert rendered["all_task_processes_exited"]
        movies, records = [], {}
        for variant in VARIANTS:
            path = folder / "render" / variant
            record = load(path / "verification.json")
            records[variant] = record
            assert record["reopened_saved_blend_checked"] and not record["draft"]
            assert record["resolution"] == [1920, 1080]
            cap = cv2.VideoCapture(str(path / f"{variant}_1080.mp4"))
            assert cap.isOpened(), variant
            fps = cap.get(cv2.CAP_PROP_FPS)
            frames, samples = 0, []
            expected = len(record["source_indices"])
            sample_indices = {0, expected // 4, expected // 2, expected - 1}
            try:
                while True:
                    ok, image = cap.read()
                    if not ok:
                        break
                    assert image.shape[:2] == (1080, 1920)
                    if frames in sample_indices:
                        assert np.std(image) > 1, "Uniform/blank decoded frame"
                        target = path / f"decoded_{frames:04}.jpg"
                        cv2.imwrite(str(target), image, [cv2.IMWRITE_JPEG_QUALITY, 92])
                        samples.append(str(target.relative_to(ROOT)))
                    frames += 1
            finally:
                cap.release()
            assert frames == expected, (variant, frames, expected)
            assert abs(fps - record["fps"]) < 1e-3
            movies.append({"variant": variant, "decoded_frames": frames, "fps": fps,
                           "resolution": [1920, 1080], "samples": samples,
                           "sha256": sha(path / f"{variant}_1080.mp4")})
        a, b = records["map_vehicles"], records["map_only"]
        assert a["source_indices"] == b["source_indices"]
        assert a["output_times"] == b["output_times"]
        for key in ("camera_matrix_world", "camera_ortho_scale"):
            assert key in a and key in b, f"Camera evidence missing: {key}"
            assert np.allclose(a[key], b[key]), f"Map cameras differ: {key}"
        for record in (a, b):
            counts = [check["visible_points"] for check in record["checks"]]
            assert all(y >= x for x, y in pairwise(counts))
            assert counts[-1] == load(folder / "data/verification.json")["point_count"]
        processes = load(folder / "render/processes.json")
        assert processes["all_owned_processes_exited"]
        assert all(r["exited"] and r["returncode"] == 0 for r in processes["records"])
        result.update(movies=movies, map_variants_same_times_and_camera=True,
                      map_point_count_monotonic=True, render_processes_recorded_exited=True)
    result.update(failures=failures, passed=not failures)
    target = folder / ("input_integrity.json" if args.inputs_only else "verification.json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
