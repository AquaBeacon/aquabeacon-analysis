"""Independent artifact checks for selected GNSS, navigation, ENU maps and previews."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import trimesh
from analyze_nanyo import read
from estimate_nanyo_absolute import n

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.mapping_frames import enu_transform
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_textured_map import camera_projection

ACQS = ["20261031_nanyo_000", "20261101_nanyo_000"]
COMBINED = "20261102_nanyo_combined_000"


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=[COMBINED])
    parser.add_argument("--skip-media", action="store_true")
    args = parser.parse_args()
    checks, media_checks = [], []

    def check(condition, name):
        assert condition, name
        checks.append(name)

    def hashes(m, name):
        for p, expected in m.get("inputs_sha256", {}).items():
            check(Path(p).is_file() and sha256(Path(p)) == expected, f"{name} input {p}")

    common = json.loads(
        Path(f"results/{ACQS[0]}/mapping_gnss_selection/manifest.json").read_text(encoding="utf-8")
    )["origin_llh"]
    for acq in ACQS:
        root = Path(f"results/{acq}")
        selection = json.loads(
            (root / "mapping_gnss_selection/manifest.json").read_text(encoding="utf-8")
        )
        hashes(selection, acq + " GNSS")
        grouped = defaultdict(list)
        for r in read(root / "mapping_gnss_selection/matched_static_positions.csv"):
            grouped[(r["window"], r["solution"])].append(r)
        scores = defaultdict(list)
        for w in selection["window_assessments"]:
            references = []
            for solution, m in w["metrics"].items():
                rows = grouped[(w["window"]["id"], solution)]
                xyz = n(rows, ["east_m", "north_m", "up_m"])
                recomputed = np.sqrt(np.mean(np.sum((xyz - xyz.mean(0)) ** 2, axis=1)))
                check(
                    abs(recomputed - m["rmse_3d_m"]) < 1e-12,
                    f"{w['window']['id']} {solution} independent 3D RMSE",
                )
                check(len({r["source_utc_s"] for r in rows}) == len(rows), "one-to-one GNSS epochs")
                check(
                    max(abs(float(r["time_delta_s"])) for r in rows) <= 0.51,
                    "GNSS matching tolerance",
                )
                references.append([r["reference_utc_s"] for r in rows])
                if w["used_for_selection"]:
                    scores[solution].append(recomputed**2)
            check(all(r == references[0] for r in references), "identical GNSS reference epochs")
            if "hand" in w["window"]["condition"]:
                check(not w["used_for_selection"], "hand shaking excluded from selection")
        scores = {k: float(np.sqrt(np.mean(v))) for k, v in scores.items()}
        check(selection["selected"] == min(scores, key=scores.get), "minimum static RMSE chosen")
        source = read(Path(selection["source"]))
        selected = read(root / "mapping_gnss_selection/selected_positions.csv")
        check(
            len(source) == len(selected)
            and all(all(b[k] == v for k, v in a.items()) for a, b in zip(source, selected)),
            "selected GNSS observations unchanged",
        )
        parent = read(root / "mapping_navigation/parent_navigation.csv")
        check(
            all(
                float(r["anchor_age_s"]) <= 5.00001
                for r in parent
                if r["position_available"] == "True"
            ),
            "parent DR limit",
        )
        hashes(
            json.loads(
                (root / "mapping_navigation/parent_manifest.json").read_text(encoding="utf-8")
            ),
            acq + " parent",
        )
        for number in range(1, 3 if acq == ACQS[0] else 6):
            eid = f"{acq[:8]}_nanyo_{number:03d}"
            base = Path(f"results/{eid}")
            lo, hi = json.loads(
                Path(f"experiments/{eid}/experiment.json").read_text(encoding="utf-8")
            )["utc_interval_s"]
            rows = read(base / "mapping_light_tracking/camera_relative_candidates.csv")
            t = n(rows, ["utc_s"])[:, 0]
            check(
                np.all(np.diff(t) > 0) and t[0] >= lo and t[-1] < hi,
                eid + " optical clock bounded and increasing",
            )
            check(
                np.max(abs(n(rows, ["segment_time_s"])[:, 0] - (t - lo))) < 1e-6,
                eid + " optical relative time",
            )
            for r in rows:
                good = r["optical_candidate"] == "True"
                check(
                    np.isfinite([float(r["x_px"]), float(r["y_px"])]).all() == good,
                    eid + " failed tracks remain NaN",
                )
            hashes(
                json.loads(
                    (base / "mapping_light_tracking/manifest.json").read_text(encoding="utf-8")
                ),
                eid + " tracking",
            )
            obs = read(base / "mapping_navigation/child_observations.csv")
            used = [r["shot_seq"] for r in obs if r["range_used"] == "True"]
            check(len(used) == len(set(used)), eid + " one range vote per shot")
            nav = read(base / "mapping_navigation/child_navigation.csv")
            check(all(r["sfm_position_used"] == "False" for r in nav), eid + " no SfM navigation")
            check(
                all(
                    float(r["anchor_age_s"]) <= 3.00001
                    for r in nav
                    if r["position_available"] == "True"
                ),
                eid + " child DR limit",
            )
            hashes(
                json.loads((base / "mapping_navigation/manifest.json").read_text(encoding="utf-8")),
                eid + " navigation",
            )
            scene = json.loads((base / "mapping_selected/scene.json").read_text(encoding="utf-8"))
            p = np.array(scene["points"]).reshape(-1, 3)
            check(np.isfinite(p).all(), eid + " finite map vertices")
            r, shift = enu_transform(scene["manifest"]["source_origin_llh"], common)
            check(
                np.max(abs(r - np.array(scene["manifest"]["R_common_from_source_enu"]))) < 1e-12,
                eid + " ENU rotation",
            )
            check(
                np.max(abs(shift - np.array(scene["manifest"]["common_translation_m"]))) < 1e-9,
                eid + " ENU origin conversion",
            )
            k, _, _ = load_camera_intrinsics(Path.cwd(), "C_MAIN_CAM", (960, 540), "proxy")
            for surf in scene["surfaces"]:
                vertices = np.array(surf["vertices"])
                check(
                    np.max(abs(vertices - p[surf["point_indices"]])) < 1e-9,
                    eid + " triangles use existing points",
                )
                px, depth = camera_projection(vertices, surf["camera"], surf["quaternion_xyzw"], k)
                expected = np.c_[px[:, 0] / 960, 1 - px[:, 1] / 540]
                check(
                    np.max(abs(expected - np.array(surf["uv"]))) < 1e-8 and np.all(depth > 0),
                    eid + " photo UV geometry",
                )
            check(
                sum(len(s["faces"]) for s in scene["surfaces"]) == scene["manifest"]["triangles"],
                eid + " triangle count",
            )
            for f in scene["manifest"]["fragments"]:
                if f["accepted"]:
                    hashes(f.get("texture_manifest", {}), eid + " texture")
                    if f.get("inputs_sha256"):
                        hashes(f, eid + " registration")
                else:
                    check(
                        (
                            base / f"mapping_selected/{f['fragment']}_UNREGISTERED_LOCAL_UNITS.ply"
                        ).is_file(),
                        eid + " rejected local geometry retained",
                    )
        if not args.skip_media:
            media_name = "navigation_replay" if acq == ACQS[0] else "mapping_replay"
            mm = json.loads((root / media_name / "media_manifest.json").read_text(encoding="utf-8"))
            for item in mm["videos"]:
                cap = cv2.VideoCapture(item["target"])
                midpoint = (item["end_s"] - item["start_s"]) / 2
                cap.set(cv2.CAP_PROP_POS_MSEC, midpoint * 1000)
                ok, frame = cap.read()
                pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
                cap.release()
                check(ok, "preview decode " + item["path"])
                source = cv2.VideoCapture(item["source"])
                source.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
                requested = item["source_start_s"] + pts / item["time_scale"]
                candidates = []
                for delta in [-0.1, -0.0667, -0.0333, 0, 0.0333, 0.0667, 0.1]:
                    source.set(cv2.CAP_PROP_POS_MSEC, (requested + delta) * 1000)
                    ok, native = source.read()
                    if ok:
                        actual = source.get(cv2.CAP_PROP_POS_MSEC) / 1000
                        mae = float(
                            np.mean(
                                abs(
                                    cv2.resize(
                                        native, (640, 360), interpolation=cv2.INTER_CUBIC
                                    ).astype(float)
                                    - frame.astype(float)
                                )
                            )
                        )
                        candidates.append((mae, actual - requested))
                source.release()
                best = min(candidates)
                check(best[0] < 15 and abs(best[1]) < 0.11, "native/preview clock " + item["path"])
                media_checks.append(
                    {
                        "video": item["path"],
                        "mean_pixel_difference": best[0],
                        "native_clock_difference_s": best[1],
                    }
                )
    for eid in [*ACQS, COMBINED]:
        out = Path(f"results/{eid}/mapping_replay")
        m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        hashes(m, eid + " replay")
        points = sum(s["points"] for s in m["segments"])
        cloud = trimesh.load(out / "points.ply")
        check(len(cloud.vertices) == points, eid + " exported PLY point count")
        glb = trimesh.load(out / "textured_map.glb", force="scene")
        check(
            sum(len(g.faces) for g in glb.geometry.values() if hasattr(g, "faces"))
            == sum(s["triangles"] for s in m["segments"]),
            eid + " exported GLB triangles",
        )
        for name in [
            "viewer.html",
            "report.html",
            "report.md",
            "mapping_overhead.png",
            "mapping_overhead.svg",
            "mapping_overhead.figure.pickle",
        ]:
            check((out / name).is_file(), eid + " deliverable " + name)
    out = Path(f"results/{COMBINED}/mapping_replay/verification.json")
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    value = {
        "all_numeric_checks_passed": True,
        "numeric_check_count": len(checks),
        "checks": sorted(set(checks)),
        "media_checks": media_checks or old.get("media_checks", []),
        "visual_checks": old.get("visual_checks", []),
        "code_sha256": sha256(Path(__file__)),
    }
    out.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        "PASS", len(checks), "checks", len(media_checks), "preview/native comparisons", flush=True
    )


if __name__ == "__main__":
    main()
