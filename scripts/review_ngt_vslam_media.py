"""Render explicit child-only video samples for visual medium review."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from aquabeacon_analysis.editable_plots import save_editable_figure

SAMPLES = {
    '20261028_ngt_003': [904, 906.6, 910, 920, 930, 940, 950, 955, 959.4, 961],
    '20261028_ngt_004': [980, 1000, 1040, 1080, 1112, 1150, 1190, 1210, 1230, 1250],
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', action='append', choices=list(SAMPLES), required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    cv2.setNumThreads(2)
    for experiment in args.experiment:
        dataset = root/f'data/interim/{experiment}/child_only_vslam/okinawa_native120'
        with (dataset/'frames.csv').open() as handle:
            rows = list(csv.DictReader(handle))
        figure, axes = plt.subplots(2, 5, figsize=(16, 4.6))
        chosen = []
        for ax, time in zip(axes.flat, SAMPLES[experiment], strict=True):
            row = min(rows, key=lambda r: abs(float(r['source_video_s'])-time))
            source = dataset/'mav0/cam0/data'/f'{row["synthetic_ns"]}.jpg'
            bgr = cv2.resize(cv2.imread(str(source)), (480, 270))
            ax.imshow(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            ax.set_title(f'{float(row["source_video_s"]):.3f} s')
            ax.axis('off')
            chosen.append({**row, 'source_image': str(source.relative_to(root))})
        figure.suptitle(experiment+' child source video: medium review (original orientation)')
        figure.tight_layout()
        output = root/f'results/{experiment}/child_only_vslam'
        save_editable_figure(figure, output/'medium_review.png', dpi=120)
        plt.close(figure)
        with (output/'medium_review_frames.csv').open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(chosen[0]))
            writer.writeheader()
            writer.writerows(chosen)


if __name__ == '__main__':
    main()
