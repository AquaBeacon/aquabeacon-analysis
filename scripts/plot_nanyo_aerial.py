import argparse
import json
from pathlib import Path

import matplotlib
import numpy as np
import requests
from PIL import Image

matplotlib.use("Agg")
import sys

import matplotlib.pyplot as plt

sys.path.insert(0, "scripts")
from analyze_nanyo import read

from aquabeacon_analysis.aerial_tracks import mercator
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256

parser = argparse.ArgumentParser(description="Plot selected Nanyo GNSS tracks on GSI imagery.")
parser.add_argument(
    "--experiment",
    action="append",
    required=True,
    choices=["20261031_nanyo_000", "20261031_nanyo_001", "20261031_nanyo_002"],
)
args = parser.parse_args()
base = Path("data/interim/20261031_nanyo_000/aerial")
base.mkdir(parents=True, exist_ok=True)
allrows = read(Path("results/20261031_nanyo_000/evaluation/ppk_positions.csv"))
xy = mercator(
    np.array([float(r["latitude_deg"]) for r in allrows]),
    np.array([float(r["longitude_deg"]) for r in allrows]),
)
center = np.median(xy, axis=0)
half = np.pi * 6378137
zoom = 17
size = 2 * half / 2**zoom
xc, yc = int((center[0] + half) // size), int((half - center[1]) // size)
mosaic = Image.new("RGB", (768, 768))
tiles = []
for x in range(xc - 1, xc + 2):
    for y in range(yc - 1, yc + 2):
        p = base / f"{zoom}_{x}_{y}.jpg"
        url = f"https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{zoom}/{x}/{y}.jpg"
        if not p.exists():
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            p.write_bytes(r.content)
        mosaic.paste(Image.open(p).convert("RGB"), ((x - xc + 1) * 256, (y - yc + 1) * 256))
        tiles.append({"url": url, "sha256": sha256(p)})
extent = [
    (xc - 1) * size - half,
    (xc + 2) * size - half,
    half - (yc + 2) * size,
    half - (yc - 1) * size,
]
full_mosaic, full_extent = mosaic, extent
for eid in args.experiment:
    mosaic, extent = full_mosaic, full_extent
    out = Path("results") / eid / "evaluation"
    bounds = []
    for label in ["spp", "ppk"]:
        rr = read(out / f"{label}_positions.csv")
        bounds.extend(
            mercator(
                [float(r["latitude_deg"]) for r in rr], [float(r["longitude_deg"]) for r in rr]
            ).tolist()
        )
    bounds = np.asarray(bounds)
    plot_center = (bounds.min(0) + bounds.max(0)) / 2
    radius = max(40, np.ptp(bounds, axis=0).max() / 2 + 20)
    if eid != "20261031_nanyo_000":
        local_zoom = 18
        local_size = 2 * half / 2**local_zoom
        local_x = int((plot_center[0] + half) // local_size)
        local_y = int((half - plot_center[1]) // local_size)
        mosaic = Image.new("RGB", (768, 768))
        for x in range(local_x - 1, local_x + 2):
            for y in range(local_y - 1, local_y + 2):
                p = base / f"{local_zoom}_{x}_{y}.jpg"
                url = f"https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{local_zoom}/{x}/{y}.jpg"
                if not p.exists():
                    response = requests.get(url, timeout=20)
                    response.raise_for_status()
                    p.write_bytes(response.content)
                mosaic.paste(
                    Image.open(p).convert("RGB"), ((x - local_x + 1) * 256, (y - local_y + 1) * 256)
                )
                tiles.append({"url": url, "sha256": sha256(p)})
        extent = [
            (local_x - 1) * local_size - half,
            (local_x + 2) * local_size - half,
            half - (local_y + 2) * local_size,
            half - (local_y - 1) * local_size,
        ]
    relative_extent = np.asarray(extent) - np.array([center[0], center[0], center[1], center[1]])
    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    for ax, label in zip(axes, ["spp", "ppk"]):
        rows = read(out / f"{label}_positions.csv")
        a = mercator(
            np.array([float(r["latitude_deg"]) for r in rows]),
            np.array([float(r["longitude_deg"]) for r in rows]),
        )
        a -= center
        ax.imshow(mosaic, extent=relative_extent)
        ax.plot(a[:, 0], a[:, 1], lw=1, color="cyan")
        ax.scatter(a[0, 0], a[0, 1], c="yellow", s=30, label="Start")
        ax.set(
            xlim=(plot_center[0] - center[0] - radius, plot_center[0] - center[0] + radius),
            ylim=(plot_center[1] - center[1] - radius, plot_center[1] - center[1] + radius),
            title=label.upper(),
            xlabel="Relative Web Mercator X [m]",
            ylabel="Relative Web Mercator Y [m]",
        )
        ax.ticklabel_format(useOffset=False, style="plain")
        ax.legend()
    fig.text(
        0.5,
        0.01,
        "Background: GSI seamlessphoto / Geospatial Information Authority of Japan. Imagery is not ground truth.",
        ha="center",
        fontsize=8,
    )
    fig.tight_layout(rect=[0, 0.03, 1, 1])
    save_editable_figure(fig, out / "gnss_aerial.png", dpi=180)
    plt.close(fig)
(base / "manifest.json").write_text(
    json.dumps(
        {
            "tiles": tiles,
            "projection": "EPSG:3857 display, not ground distance",
            "attribution": "国土地理院 シームレス空中写真",
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
