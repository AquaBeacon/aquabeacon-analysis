"""Create a CLAHE image segment for a provisional ORB-SLAM3 pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import cv2


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(experiment_id: str, start: int, end: int, root: Path,
            dataset_root: Path, output_root: Path) -> Path:
    source = dataset_root / experiment_id
    destination = output_root / experiment_id
    times = (source / "timestamps.txt").read_text(encoding="utf-8").splitlines()
    if not 0 <= start < end <= len(times):
        raise ValueError(f"invalid frame range: {start}:{end} of {len(times)}")
    if destination.exists():
        raise FileExistsError(f"trial output already exists: {destination}")
    image_dir = destination / "mav0/cam0/data"
    image_dir.mkdir(parents=True)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    for timestamp in times[start:end]:
        image_path = source / "mav0/cam0/data" / f"{timestamp}.png"
        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"missing source image: {image_path}")
        if not cv2.imwrite(str(image_dir / f"{timestamp}.png"), clahe.apply(image)):
            raise OSError(f"failed to write CLAHE image: {timestamp}")
    (destination / "timestamps.txt").write_bytes(
        ("\n".join(times[start:end]) + "\n").encode("ascii"))
    for relative in ("mav0/imu0/data.csv", "mav0/pressure0/data.csv"):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    source_manifest = source / "export_manifest.json"
    export_manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    export_manifest.update({
        "test_only": True,
        "frame_alignment_note": (
            f"CLAHE trial of verified source alignment, image indices [{start},{end}); "
            + str(export_manifest["frame_alignment_note"])),
        "clahe_clip_limit": 2.0,
        "clahe_tile_grid": [8, 8],
        "source_export_manifest_sha256": sha256(source_manifest),
    })
    (destination / "export_manifest.json").write_text(
        json.dumps(export_manifest, indent=2), encoding="utf-8")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                            capture_output=True, text=True, check=False)
    report = {
        "experiment_id": experiment_id,
        "source_dataset": str(source),
        "start_image": start,
        "end_image_exclusive": end,
        "frame_count": end - start,
        "image_processing": "CLAHE clipLimit=2.0 tileGridSize=8x8, grayscale PNG",
        "source_imu_sha256": sha256(source / "mav0/imu0/data.csv"),
        "git_commit": commit.stdout.strip() if commit.returncode == 0 else None,
    }
    (destination / "trial_manifest.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    parser.add_argument("--start-image", type=int, required=True)
    parser.add_argument("--end-image", type=int, required=True)
    parser.add_argument("--dataset-root", type=Path, default=Path("data/interim/orbslam3_full"))
    parser.add_argument("--output-root", type=Path, default=Path("data/interim/orbslam3_clahe_trial"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(prepare(args.experiment_id, args.start_image, args.end_image,
                  root, args.dataset_root, args.output_root))


if __name__ == "__main__":
    main()
