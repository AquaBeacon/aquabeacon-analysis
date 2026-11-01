"""Exploratory static-window selection independent of relative-position residuals."""

import argparse
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from evaluate_nanyo_static import array, read, rotations
from scipy.spatial.transform import Rotation

from aquabeacon_analysis.editable_plots import save_editable_figure
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.static_evaluation import static_metrics


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    eid = args.experiment
    paths = [
        Path(f"results/{eid}/absolute_position/positions.csv"),
        Path(f"data/processed/{eid}/absolute_position/attitudes_10hz.csv"),
        Path(
            "data/processed/20261031_nanyo_000/synchronization/"
            "child_DJI_20261031142636_0026_D_attitude.csv"
        ),
        Path(f"results/{eid}/absolute_position/manifest.json"),
    ]
    rows, attitudes, camera = [read(p) for p in paths[:3]]
    cfg = json.loads(paths[3].read_text(encoding="utf-8"))["configuration"]
    times = array(rows, ["utc_s"])[:, 0]
    ct = array(camera, ["sync_utc_s"])[:, 0]
    cq = array(camera, ["qx", "qy", "qz", "qw"])
    # Whole-clock 10-second grid. Search first six minutes after segment start.
    first = np.ceil(times[0] / 10) * 10
    candidates = []
    for lo in np.arange(first, times[0] + 360 - 60 + 1, 10):
        selected = [r for r in rows if lo <= float(r["utc_s"]) < lo + 60]
        child_rows = [
            r
            for r in attitudes
            if lo <= float(r["utc_s"]) < lo + 60 and r["child_attitude_available"] == "True"
        ]
        child_rot = rotations(child_rows, "child")
        angles = np.degrees((child_rot.mean().inv() * child_rot).magnitude())
        camera_rot = Rotation.from_quat(cq[(ct >= lo) & (ct < lo + 60)])
        camera_angles = np.degrees((camera_rot.mean().inv() * camera_rot).magnitude())
        parent_rows = [
            r
            for r in selected
            if all(
                np.isfinite(float(r[k]))
                for k in [f"parent_antenna_{a}_m" for a in ["east", "north", "up"]]
                + [f"parent_true_enu_q{a}" for a in "xyzw"]
            )
        ]
        pg = array(parent_rows, [f"parent_antenna_{a}_m" for a in ["east", "north", "up"]])
        parent = pg - rotations(parent_rows, "parent").apply(
            cfg["lever_arms_frd_m"]["parent_antenna"]
        )
        pm = static_metrics(parent)
        depths = array(selected, ["pressure_depth_m"])[:, 0]
        quality = array(parent_rows, ["ppk_quality"])[:, 0]
        candidates.append(
            {
                "start": datetime.fromtimestamp(lo, timezone(timedelta(hours=9))).isoformat(),
                "utc_s": float(lo),
                "child_bno_rms_deg": float(np.sqrt(np.mean(angles**2))),
                "child_camera_rms_deg": float(np.sqrt(np.mean(camera_angles**2))),
                "parent_horizontal_rmse_m": pm["horizontal_rmse_m"],
                "parent_3d_rmse_m": pm["rmse_3d_m"],
                "parent_samples": len(parent_rows),
                "samples": len(selected),
                "position_available": sum(r["position_available"] == "True" for r in selected),
                "pressure_mean_m": float(np.nanmean(depths)),
                "pressure_std_m": float(np.nanstd(depths)),
                "ppk_fixed_fraction": float(np.mean(quality == 1)),
            }
        )
    # Exploratory thresholds chosen after an initial inspection, not accuracy limits.
    eligible = [
        r
        for r in candidates
        if max(r["child_bno_rms_deg"], r["child_camera_rms_deg"]) <= 6
        and r["parent_samples"] >= 0.95 * r["samples"]
    ]
    if not eligible:
        raise ValueError("No candidate meets exploratory attitude/coverage criteria")
    chosen = min(eligible, key=lambda r: r["parent_3d_rmse_m"])
    out = Path(f"results/{eid}/static_selection")
    out.mkdir(parents=True, exist_ok=True)
    with (out / "candidates.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(chosen))
        writer.writeheader()
        writer.writerows(candidates)
    info = {
        "experiment_id": eid,
        "duration_s": 60,
        "search_first_s": 360,
        "step_s": 10,
        "selection_rule": "Exploratory: both child attitude RMS <=6 deg, parent coverage >=95%; "
        "minimum parent CAD-origin 3D scatter. No relative-position RMSE used.",
        "threshold_status": "Chosen after initial inspection; not preregistered or validated static limits",
        "selected": chosen,
        "eligible_count": len(eligible),
        "inputs_sha256": {str(p): sha256(p) for p in paths + [Path(__file__)]},
        "upstream_configuration": cfg,
    }
    (out / "selection.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    x = (np.array([r["utc_s"] for r in candidates]) - times[0]) / 60
    fig, axs = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    for key, label in [
        ("child_bno_rms_deg", "Child BNO"),
        ("child_camera_rms_deg", "Child camera"),
    ]:
        axs[0].plot(x, [r[key] for r in candidates], ".-", label=label)
    axs[0].axhline(6, color="gray", linestyle="--", label="Exploratory threshold")
    axs[0].set_ylabel("Attitude RMS (deg)")
    axs[0].legend()
    axs[1].plot(x, [r["parent_3d_rmse_m"] for r in candidates], ".-", label="Parent position 3D")
    axs[1].set_ylabel("Position scatter (m)")
    for ax in axs:
        ax.axvline((chosen["utc_s"] - times[0]) / 60, color="green", linestyle=":")
        ax.grid(alpha=0.3)
    axs[1].set_xlabel("60-second window start, minutes after segment start")
    fig.tight_layout()
    save_editable_figure(fig, out / "selection.png", dpi=170)
    plt.close(fig)
    report = [
        f"# 区間①の初期静止候補：{eid}",
        "",
        f"採用：{chosen['start']} から60秒。",
        "区間開始約4分10秒後。投入直後というユーザーの手掛かりから、区間開始後6分に収まる60秒窓を10秒刻みで探索。",
        "最初の下降中の区間には相対位置の有効観測がなく、14:41頃に姿勢・親機位置・水深が比較的安定した。",
        "",
        "選定条件は初期探索後に設定した探索的基準：子機BNOとカメラの平均回転周りRMSがともに6°以下、",
        "親機位置・姿勢の有効率95%以上。その候補のうち親機CAD原点位置の3Dばらつき最小を採用。",
        "相対位置RMSEを選定に使っていない。ただし事前登録した判定閾値ではなく、完全静止を保証しない。",
        "",
        f"- 子機姿勢RMS：BNO {chosen['child_bno_rms_deg']:.3f}°、カメラ {chosen['child_camera_rms_deg']:.3f}°。",
        f"- 親機位置（全120観測）水平RMSE {chosen['parent_horizontal_rmse_m']:.4f} m、3D {chosen['parent_3d_rmse_m']:.4f} m。",
        f"- 子機圧力深度：平均 {chosen['pressure_mean_m']:.4f} m、標準偏差 {chosen['pressure_std_m']:.4f} m。",
        f"- 相対位置有効観測：{chosen['position_available']}/{chosen['samples']}。",
        "",
        "親機位置はPPK推定のばらつきであり、真の並進量とは分離できない。今回の採用窓は全点Float解。",
        "親機の波による動揺、子機の残留回転、光点・測距・姿勢・同期誤差を含む条件付き評価。",
        "",
        "![候補探索](selection.png)",
        "",
        "[全候補CSV](candidates.csv) / [選定条件・来歴](selection.json) / [相対位置RMSE](../static_evaluation/report.md)",
    ]
    (out / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(info["selected"], indent=2))


if __name__ == "__main__":
    main()
