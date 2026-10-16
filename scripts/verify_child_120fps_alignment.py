"""Compare 30 Hz proxy anchors against every fourth native 120 Hz frame."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def inspect(root: Path, experiment_id: str) -> Path:
    folder = root / "data/processed" / experiment_id / "tof_window/camera_video"
    proxy_path = next(folder.glob("C_MAIN*proxy.mp4"))
    source_path = next(folder.glob("C_MAIN*video.mp4"))
    proxy = cv2.VideoCapture(str(proxy_path))
    source = cv2.VideoCapture(str(source_path))
    count = int(proxy.get(cv2.CAP_PROP_FRAME_COUNT))
    samples = []
    for index in np.linspace(0, count - 10, 5, dtype=int):
        proxy.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, reference = proxy.read()
        if not ok:
            raise ValueError(f"cannot read proxy frame {index}")
        reference = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY).astype(np.int16)
        errors = {}
        for offset in (-1, 0, 1):
            target = 4 * int(index) + offset
            if target < 0:
                continue
            source.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, frame = source.read()
            if not ok:
                raise ValueError(f"cannot read source frame {target}")
            gray = cv2.cvtColor(cv2.resize(frame, (1280, 720)),
                                    cv2.COLOR_BGR2GRAY).astype(np.int16)
            errors[str(offset)] = round(float(np.mean(np.abs(reference - gray))), 4)
        if errors["0"] > min(errors.values()) + 0.5:
            raise ValueError(f"fourfold alignment failed at proxy {index}: {errors}")
        samples.append({"proxy_frame": int(index), "source_frame": 4 * int(index),
                        "mean_absolute_gray_error_by_source_offset": errors})
    proxy.release()
    source.release()
    output = root / "results" / experiment_id / "vislam_depth/frame_alignment_120fps.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"experiment_id": experiment_id,
                                  "source_video": str(source_path),
                                  "proxy_video": str(proxy_path),
                                  "proxy_fps": 30000 / 1001,
                                  "source_fps": 120000 / 1001,
                                  "samples": samples}, indent=2), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment_id")
    args = parser.parse_args()
    print(inspect(Path(__file__).resolve().parents[1], args.experiment_id))


if __name__ == "__main__":
    main()
