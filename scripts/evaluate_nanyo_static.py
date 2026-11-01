"""Evaluate a user-declared static interval without fitting away drift or waves."""

import argparse
import csv
import json
import subprocess
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.nanyo_position import ray_sphere_position
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_position import calibrated_rays
from aquabeacon_analysis.static_evaluation import static_metrics


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def array(rows, keys):
    return np.array([[float(r[k]) for k in keys] for r in rows])


def rotations(rows, role):
    return Rotation.from_quat(array(rows, [f"{role}_true_enu_q{k}" for k in "xyzw"]))


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--start", default="2026-10-31T15:03:00+09:00")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument(
        "--selection-note", default="User reports child grounded and parent not deliberately moved"
    )
    args = parser.parse_args()
    start = datetime.fromisoformat(args.start)
    if start.utcoffset() is None or args.duration <= 0:
        raise ValueError("Explicit timezone and positive duration required")
    lo, hi = start.timestamp(), start.timestamp() + args.duration
    base = Path(f"results/{args.experiment}")
    out = base / "static_evaluation"
    out.mkdir(parents=True, exist_ok=True)
    source = base / "absolute_position/positions.csv"
    upstream = base / "absolute_position/manifest.json"
    # Use the configuration actually embedded in the source analysis.
    manifest = json.loads(upstream.read_text(encoding="utf-8"))
    cfg = manifest["configuration"]
    rows = [r for r in read(source) if lo <= float(r["utc_s"]) < hi]
    good = [r for r in rows if r["position_available"] == "True"]
    if len(good) < 2:
        raise ValueError("Fewer than two valid position observations")
    t = array(good, ["utc_s"])[:, 0]
    rp, rc = rotations(good, "parent"), rotations(good, "child")
    pg = array(good, [f"parent_antenna_{a}_m" for a in ["east", "north", "up"]])
    child = array(good, [f"child_origin_{a}_m" for a in ["east", "north", "up"]])
    lever = {k: np.array(v) for k, v in cfg["lever_arms_frd_m"].items() if k != "child_imu"}
    parent = pg - rp.apply(lever["parent_antenna"])
    relative = child - parent
    attitude_path = Path(f"data/processed/{args.experiment}/absolute_position/attitudes_10hz.csv")
    attitudes = [r for r in read(attitude_path) if lo <= float(r["utc_s"]) < hi]
    attitude_metrics = {}
    for role in ["parent", "child"]:
        valid = [r for r in attitudes if r[f"{role}_attitude_available"] == "True"]
        rot = rotations(valid, role)
        angles = np.degrees((rot.mean().inv() * rot).magnitude())
        attitude_metrics[role] = {
            "n": len(valid),
            "rms_deg": float(np.sqrt(np.mean(angles**2))),
            "p95_deg": float(np.percentile(angles, 95)),
            "max_deg": float(angles.max()),
        }
    parent_valid = [r for r in attitudes if r["parent_attitude_available"] == "True"]
    camera_path = Path(
        "data/processed/20261031_nanyo_000/synchronization/"
        "child_DJI_20261031142636_0026_D_attitude.csv"
    )
    camera_rows = [r for r in read(camera_path) if lo <= float(r["sync_utc_s"]) < hi]
    camera_rot = Rotation.from_quat(array(camera_rows, ["qx", "qy", "qz", "qw"]))
    camera_angles = np.degrees((camera_rot.mean().inv() * camera_rot).magnitude())
    attitude_metrics["child_camera_independent"] = {
        "n": len(camera_rows),
        "rms_deg": float(np.sqrt(np.mean(camera_angles**2))),
        "max_deg": float(camera_angles.max()),
    }
    fixed = rotations(parent_valid, "parent").mean().as_matrix()
    k, distortion, provenance = load_camera_intrinsics(
        Path.cwd(), "P_MAIN_CAM", (1280, 720), "proxy"
    )
    rays = calibrated_rays(array(good, ["x_px", "y_px"]), k, distortion)
    axes_path = Path(cfg["parent_axes"])
    axes = np.array(json.loads(axes_path.read_text())["R_body_from_spresense"])
    frozen, recomputed = [], []
    for i, row in enumerate(good):
        mount = cfg["mounts"]["parent_" + Path(row["source_video"]).stem]
        body_opt = (
            axes
            @ np.array(mount["R_dji_from_sensor"]).T
            @ np.array(cfg["R_dji_from_parent_optical"])
        )
        for rotation, target in [(fixed, frozen), (rp[i].as_matrix(), recomputed)]:
            origin, _, _, _ = ray_sphere_position(
                np.zeros(3),
                rotation,
                rc[i].as_matrix(),
                rays[i],
                body_opt,
                float(row["range_m"]),
                **lever,
            )
            target.append(origin + rotation @ lever["parent_antenna"])
    frozen = np.array(frozen)
    assert np.allclose(recomputed, relative, atol=1e-8, rtol=0), "Upstream geometry mismatch"
    metrics = {
        "compensated_relative_enu": static_metrics(relative),
        "fixed_parent_attitude_relative_enu": static_metrics(frozen),
        "parent_origin_enu_diagnostic": static_metrics(parent),
        "child_origin_enu_diagnostic": static_metrics(child),
    }
    delta = relative - frozen
    metrics["attitude_correction_variation_rmse_m"] = static_metrics(delta)["rmse_3d_m"]
    range_values = array(good, ["range_m"])[:, 0]
    metrics["range_mean_m"] = float(range_values.mean())
    metrics["range_rmse_m"] = float(range_values.std())
    metrics["attitude_10hz"] = attitude_metrics
    metrics["subwindows"] = []
    for offset in [0, 10, 20, 30]:
        mask = (t >= lo + offset) & (t < min(hi, lo + offset + 30))
        if mask.sum() > 1:
            metrics["subwindows"].append(
                dict(start_offset_s=offset, duration_s=30, **static_metrics(relative[mask]))
            )
    exported = []
    index = {float(r["utc_s"]): i for i, r in enumerate(good)}
    for row in rows:
        item = dict(row)
        i = index.get(float(row["utc_s"]))
        for label, values in [("relative", relative), ("fixed_attitude_relative", frozen)]:
            for j, axis in enumerate(["east", "north", "up"]):
                item[f"{label}_{axis}_m"] = values[i, j] if i is not None else ""
        exported.append(item)
    with (out / "samples.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(exported[0]))
        writer.writeheader()
        writer.writerows(exported)
    fig, axs = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    for j, ax in enumerate(axs):
        for values, label in [
            (relative, "Measured parent attitude"),
            (frozen, "Parent attitude held constant"),
        ]:
            ax.plot(t - lo, values[:, j] - values[:, j].mean(), ".", label=label, ms=4)
        ax.set_ylabel(["East residual (m)", "North residual (m)", "Up residual (m)"][j])
        ax.grid(alpha=0.3)
    axs[0].legend()
    axs[-1].set_xlabel(f"Seconds after {args.start} (missing samples not interpolated)")
    fig.tight_layout()
    save_editable_figure(fig, out / "static_residuals.png", dpi=170)
    plt.close(fig)
    inputs = [
        source,
        upstream,
        attitude_path,
        camera_path,
        axes_path,
        Path(__file__),
        Path("src/aquabeacon_analysis/static_evaluation.py"),
    ]
    result = {
        "experiment_id": args.experiment,
        "start": args.start,
        "duration_s": args.duration,
        "interval": "[start,end)",
        "candidate_samples": len(rows),
        "valid_samples": len(good),
        "coverage": len(good) / len(rows),
        "unique_shots": len({r["shot_seq"] for r in good}),
        "first_valid_offset_s": float(t[0] - lo),
        "last_valid_offset_s": float(t[-1] - lo),
        "maximum_valid_sample_gap_s": float(np.diff(t).max()),
        "reference": "own arithmetic mean; population denominator N; no detrending",
        "frame": "child CAD body origin minus parent CAD body origin, true ENU",
        "selection": "upstream position_available; no extra outlier removal or smoothing",
        "static_assumption": args.selection_note,
        "accuracy_validated": False,
        "configuration": cfg,
        "intrinsics": provenance,
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], text=True),
        "input_sha256": {str(p): sha256(p) for p in inputs},
        "metrics": metrics,
    }
    (out / "manifest.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    m, f = metrics["compensated_relative_enu"], metrics["fixed_parent_attitude_relative_enu"]
    lines = [
        f"# 静止相対位置評価：{args.experiment}",
        "",
        f"区間：{args.start} から {args.duration:g} 秒（終端を含まない）。",
        (
            f"有効観測 {len(good)}/{len(rows)} 点、{result['coverage']:.1%}。"
            f"最大有効点間隔 {result['maximum_valid_sample_gap_s']:.2f} s。"
        ),
        "",
        (
            "親機CAD機体原点から子機CAD機体原点へのベクトルを真北ENUで評価。"
            "GNSS共通並進を差し引き、親機アンテナのレバーアームを補正した。"
        ),
        args.selection_note + "。姿勢一定だけでは並進静止は証明できない。",
        "",
        (
            "RMSE = sqrt(mean(||r − mean(r)||²))。軸別も分母N。"
            "真値に対する絶対精度ではなく、静止仮定下のばらつき。外れ値追加除去・平滑化・トレンド除去なし。"
        ),
        "",
        "| 条件 | 東RMSE m | 北RMSE m | 上RMSE m | 水平RMSE m | 3D RMSE m |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for title, item in [("親機姿勢補正あり", m), ("親機姿勢を区間平均に固定", f)]:
        lines.append(
            "| "
            + title
            + " | "
            + " | ".join(
                f"{v:.4f}"
                for v in [*item["axis_rmse_m"], item["horizontal_rmse_m"], item["rmse_3d_m"]]
            )
            + " |"
        )
    lines += [
        "",
        f"平均相対位置（東・北・上）m：{m['mean_m']}。",
        f"平均からの3D偏差95%点 {m['radius_p95_m']:.4f} m、最大 {m['radius_max_m']:.4f} m。",
        f"音響距離：平均 {metrics['range_mean_m']:.4f} m、平均周りRMSE {metrics['range_rmse_m']:.4f} m。",
        "",
        (
            "姿勢補正なしの比較は同じ有効観測・距離・子機姿勢を使い、親機回転だけを10 Hz区間平均に固定して幾何を再計算。"
            "各条件それぞれの平均周りで評価するため、一定バイアスは評価できない。"
        ),
        "",
        "10 Hz姿勢の平均回転からの角度RMS："
        + "、".join(f"{role} {item['rms_deg']:.3f}°" for role, item in attitude_metrics.items())
        + "。",
        (
            "child_camera_independentは子機カメラの別姿勢ログ（元サンプリング周期）。"
            "BNO絶対姿勢とカメラ姿勢の変動量を比較し、着底の申告とログを区別する。"
        ),
        (
            "姿勢変動の数値は上記の区間実測値を参照。"
            "完全静止を保証するものではなく、結果は選択区間の条件付き静止RMSEである。"
        ),
        (
            "親機の実際の波による並進・回転、磁気方位誤差、測距・光点検出・同期誤差を含む。"
            "比較差を波だけの寄与率や独立した誤差分散と解釈しない。CAD測定中心・校正も暫定。"
        ),
        "",
        "![静止残差](static_residuals.png)",
        "",
        "[全観測CSV](samples.csv) / [来歴・30秒窓感度・姿勢診断](manifest.json)",
        "",
        (
            "検証：元の位置解をGNSS並進なしで再計算し1e-8 m許容差で一致。"
            "欠測行をCSVに保持。入力・コードSHA-256、上流manifest、実使用設定を記録。"
        ),
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: result[k] for k in ["candidate_samples", "valid_samples", "metrics"]}, indent=2
        )
    )


if __name__ == "__main__":
    main()
