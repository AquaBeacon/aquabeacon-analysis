"""Refresh embedded trajectory data in the task's existing 3D visualization."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


def refresh(path: Path, root: Path) -> None:
    html = path.read_text(encoding="utf-8")
    before, suffix = html.split("data=", 1)
    _, after = suffix.split(",q=s=>", 1)
    data = {}
    for number in (22, 23, 24):
        experiment = f"20260922_cmain_{number:03d}"
        directory = root / "results" / experiment / "relative_position"
        with (directory / "fused_position.csv").open(encoding="utf-8", newline="") as stream:
            source = list(csv.DictReader(stream))
        manifest = json.loads((directory / "fused_position_manifest.json").read_text(encoding="utf-8"))
        rows = []
        for row in source:
            values = [row[key] for key in (
                "video_time_s", "fused_x_m", "fused_y_m", "fused_z_up_m",
                "pressure_referenced_observation_x_m",
                "pressure_referenced_observation_y_m",
                "pressure_referenced_observation_z_up_m",
            )]
            rows.append([round(float(value), 3) if value else None for value in values]
                        + [int(bool(row["pressure_referenced_observation_x_m"]))])
        data[str(number)] = {"rows": rows, "pressure": manifest["pressure_used_in_position"]}
    html = before + "data=" + json.dumps(data, separators=(",", ":")) + ",q=s=>" + after
    html = html.replace("圧力の採否", "鉛直位置の基準")
    html = html.replace("ToF＋ライト観測", "ToF＋ライト水平・圧力深度観測")
    html = re.sub(
        r'<div class="text-small text-muted">(?:水中カメラ画角・取付回転|鉛直位置).*?</div>',
        '<div class="text-small text-muted">原点は親機カメラ、+zは上。橙色の観測点も'
        'ToF＋ライトの水平位置と圧力深度を組み合わせ、青の融合軌跡と同じ座標基準で示します。'
        '仮のライト仰角から得た鉛直値は表示せずCSVに診断値として残します。'
        '画角・取付回転とセンサ間の高さ差は未校正です。</div>',
        html,
    )
    html = html.replace("光学観測から2秒超の欠測", "圧力またはライト観測の欠測")
    path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    refresh(args.path, Path(__file__).resolve().parents[1])
