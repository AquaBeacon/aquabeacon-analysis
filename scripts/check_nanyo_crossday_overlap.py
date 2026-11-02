"""Look for cross-day image correspondences without fitting an arbitrary map offset."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import pycolmap as pc
from analyze_nanyo import write
from scipy.spatial import cKDTree

from aquabeacon_analysis.ngt_binary import sha256


def representatives(day):
    result = []
    for number in range(1, 3 if day == "20261031" else 6):
        eid = f"{day}_nanyo_{number:03d}"
        if day == "20261031":
            models = [
                Path(
                    f"data/processed/{eid}/position_mapping/{'extended_0' if number == 1 else 'original_1'}"
                )
            ]
        else:
            models = sorted(Path(f"data/interim/{eid}/visual_mapping/models").iterdir())
        root = Path(
            f"data/interim/{eid}/{'position_mapping' if eid == '20261031_nanyo_001' else 'visual_mapping'}/images"
        )
        for source in models:
            model = pc.Reconstruction(source)
            images = sorted(model.images.values(), key=lambda im: im.name)
            count = min(len(images), 12 if day == "20261031" and number == 1 else 4)
            for idx in np.unique(np.linspace(0, len(images) - 1, count).astype(int)):
                im = images[idx]
                path = root / im.name
                frame = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                keypoints, descriptors = cv2.SIFT_create(nfeatures=2500).detectAndCompute(
                    frame, None
                )
                points = [p for p in im.points2D if p.has_point3D()]
                tree = cKDTree(np.array([p.xy for p in points])) if points else None
                result.append(
                    {
                        "id": f"{eid}/{source.name}/{im.name}",
                        "path": path,
                        "keypoints": keypoints,
                        "descriptors": descriptors,
                        "point_tree": tree,
                        "points": points,
                        "model": model,
                    }
                )
    return result


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261102_nanyo_combined_000"])
    eid = parser.parse_args().experiment
    out = Path(f"results/{eid}/mapping_replay/crossday_overlap")
    out.mkdir(parents=True, exist_ok=True)
    cv2.setNumThreads(2)
    cv2.setRNGSeed(42)
    left, right = representatives("20261031"), representatives("20261101")
    rows, best = [], []
    for i, a in enumerate(left):
        for b in right:
            if a["descriptors"] is None or b["descriptors"] is None:
                continue
            flann = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 4}, {"checks": 64})
            pairs = flann.knnMatch(a["descriptors"], b["descriptors"], k=2)
            matches = [m for m, n in pairs if m.distance < 0.7 * n.distance]
            reverse = flann.knnMatch(b["descriptors"], a["descriptors"], k=2)
            reverse_pairs = {
                (m.trainIdx, m.queryIdx) for m, n in reverse if m.distance < 0.7 * n.distance
            }
            matches = [m for m in matches if (m.queryIdx, m.trainIdx) in reverse_pairs]
            xy_a = np.array([a["keypoints"][m.queryIdx].pt for m in matches], float)
            xy_b = np.array([b["keypoints"][m.trainIdx].pt for m in matches], float)
            keep = np.zeros(len(matches), bool)
            if len(matches) >= 12:
                _, mask = cv2.findFundamentalMat(xy_a, xy_b, cv2.USAC_MAGSAC, 1.5, 0.999, 10000)
                if mask is not None:
                    keep = mask[:, 0].astype(bool)
            inliers = int(keep.sum())
            spread = (
                min(np.prod(np.ptp(x[keep], axis=0)) / (960 * 540) for x in [xy_a, xy_b])
                if inliers
                else 0.0
            )
            three_d = 0
            if inliers and a["point_tree"] is not None and b["point_tree"] is not None:
                da, ia = a["point_tree"].query(xy_a[keep], distance_upper_bound=2)
                db, ib = b["point_tree"].query(xy_b[keep], distance_upper_bound=2)
                valid = np.isfinite(da) & np.isfinite(db)
                unique = {
                    (a["points"][j].point3D_id, b["points"][k].point3D_id)
                    for j, k in zip(ia[valid], ib[valid])
                }
                three_d = len(unique)
            candidate = (
                inliers >= 30
                and inliers / max(1, len(matches)) >= 0.25
                and spread >= 0.08
                and three_d >= 12
            )
            row = {
                "day1_image": a["id"],
                "day2_image": b["id"],
                "mutual_ratio_matches": len(matches),
                "epipolar_inliers": inliers,
                "image_area_fraction": spread,
                "linked_3d_pairs": three_d,
                "stitch_candidate": candidate,
            }
            rows.append(row)
            best.append((inliers, len(matches), a, b, matches, keep))
            best.sort(key=lambda v: v[:2], reverse=True)
            best = best[:6]
        print("CROSSDAY", i + 1, "/", len(left), "best", best[0][:2], flush=True)
    write(out / "image_pairs.csv", rows)
    for i, (_, _, a, b, matches, keep) in enumerate(best):
        img = cv2.drawMatches(
            cv2.imread(str(a["path"])),
            a["keypoints"],
            cv2.imread(str(b["path"])),
            b["keypoints"],
            [m for m, k in zip(matches, keep) if k],
            None,
            flags=cv2.DrawMatchesFlags_NOT_DRAW_SINGLE_POINTS,
        )
        cv2.putText(
            img, f"{a['id']} -> {b['id']}", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 1
        )
        cv2.imwrite(str(out / f"best_pair_{i:02d}.jpg"), img)
    manifest = {
        "experiment_id": eid,
        "day1_images": len(left),
        "day2_images": len(right),
        "image_pairs": len(rows),
        "candidate_pairs": sum(r["stitch_candidate"] for r in rows),
        "maximum_epipolar_inliers": max(r["epipolar_inliers"] for r in rows),
        "visual_stitch_accepted": False,
        "search_scope": "Evenly selected registered images from every available trial/model; not an exhaustive test of all original video frames",
        "method": "SIFT <=2500, mutual Lowe ratio <0.70, fundamental USAC_MAGSAC 1.5px; candidate >=30 inliers, >=25% inlier ratio, >=8% image coverage, >=12 unique SfM 3D pairs",
        "negative_result_scope": "No accepted stitch does not prove absence of overlap. Dynamic objects/repetitive ropes are not static landmarks.",
        "inputs_sha256": {
            str(p): sha256(p) for p in [*[a["path"] for a in left + right], Path(__file__)]
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(manifest["candidate_pairs"], "candidate pairs", flush=True)


if __name__ == "__main__":
    main()
