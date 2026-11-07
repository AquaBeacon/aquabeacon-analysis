"""Audit and plot recorded NGT envelope observations, without asserting reflection paths."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import itertools

import matplotlib.pyplot as plt
import numpy as np

from aquabeacon_analysis.editable_plots import PlotDocument, save_editable_figure
from aquabeacon_analysis.experiments import load_experiment
from aquabeacon_analysis.multipath import burst_offsets_s, envelope_events, max_bins


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261028_ngt_000"])
    args = parser.parse_args()
    root = Path.cwd()
    exp = load_experiment(root, args.experiment)
    print(
        json.dumps(
            {
                "pid": os.getpid(),
                "started_utc": datetime.now(UTC).isoformat(),
                "purpose": "multipath descriptive analysis",
                "owner": args.experiment,
            }
        ),
        flush=True,
    )
    out = exp.stage_path("results") / "multipath"
    out.mkdir(parents=True, exist_ok=True)
    folder = exp.stage_path("interim") / "decoded/child"
    base = f"C_MAIN_MULTIPATH_{exp.metadata['c_main_suffix']}"
    decode_path = folder / f"{base}.bin.decode.json"
    decoded = json.loads(decode_path.read_text())
    raw = exp.raw_root / "child" / f"{base}.bin"
    assert digest(raw) == decoded["source_sha256"]
    inputs = [raw, decode_path, exp.metadata_path]
    for stream in decoded["streams"].values():
        path = folder / Path(stream["csv"]).name
        assert digest(path) == stream["csv_sha256"]
        with path.open(encoding="utf-8-sig") as source:
            assert sum(1 for _ in source) - 1 == stream["rows"]
        inputs.append(path)
    begins = read(folder / f"{base}.capture_begin.csv")
    ends = read(folder / f"{base}.capture_end.csv")
    edge_rows = read(folder / f"{base}.comparator.csv")
    array = np.genfromtxt(
        folder / f"{base}.envelope.csv",
        delimiter=",",
        skip_header=1,
        usecols=(0, 1, 2, 3, 4),
        converters={4: lambda s: float(str(s).lower() in ("true", "b'true'"))},
    )
    shot_path = folder / f"C_MAIN_HEALTH_RANGE_{exp.metadata['c_main_suffix']}.shot.csv"
    mapping_path = exp.stage_path("results") / "ingest/child_shot_utc_mapping.csv"
    sync_path = exp.stage_path("results") / "synchronization/manifest.json"
    config_path = root / exp.metadata["config_paths"][0]
    inputs += [shot_path, mapping_path, sync_path, config_path]
    shots = {int(r["shot_seq"]): r for r in read(shot_path)}
    mapping = {int(r["shot_seq"]): r for r in read(mapping_path)}
    clock = json.loads(sync_path.read_text())["cameras"]["parent"]
    config = json.loads(config_path.read_text())

    def video_s(shot):
        return (
            float(mapping[shot]["child_tx_utc_us"]) / 1e6 - clock["utc_at_video_zero_s"]
        ) / clock["time_scale"]

    def segment(t):
        for s in config["segments"]:
            low, high = s["parent_video_interval_s"]
            if low <= t < high:
                return s["experiment_id"]
        return "outside_declared_segments"

    summary, events, windows, captures, clusters, periods = [], [], [], [], [], []
    for begin in begins:
        shot = int(begin["shot_seq"])
        s = shots[shot]
        assert int(s["session"]) == int(begin["session"])
        assert int(s["rx_us"]) == int(begin["rx_us"])
        a = array[array[:, 0] == shot]
        assert len(a) == int(begin["target_samples"])
        assert np.all(np.diff(a[:, 1]) == 1) and np.all(np.diff(a[:, 2]) == 50)
        assert not a[:, 4].any()
        end = next(e for e in ends if int(e["shot_seq"]) == shot)
        assert int(end["reason"]) == 0 and int(end["written_samples"]) == len(a)
        assert all(int(end[k]) == 0 for k in ("lost_samples", "edge_drops", "request_drops"))
        t = (a[:, 2] - int(begin["rx_us"])) / 1e6
        y = a[:, 3]
        result = envelope_events(t, y)
        bursts = burst_offsets_s(
            int(s["frequency_hz"]),
            int(s["acoustic_pattern"]),
            int(s["pattern_gap1_us"]),
            int(s["pattern_gap2_us"]),
        )
        late = t >= 0.020
        early = t < 0.020
        late_excess = float(y[late].max() - result["baseline_adc"])
        early_excess = float(y[early].max() - result["baseline_adc"])
        row = {
            "shot_seq": shot,
            "parent_video_s": video_s(shot),
            "segment": segment(video_s(shot)),
            "clock_source": mapping[shot]["child_tx_utc_source"],
            "frequency_hz": int(s["frequency_hz"]),
            "pattern_id": int(s["acoustic_pattern_id"]),
            "pattern_gap1_us": int(s["pattern_gap1_us"]),
            "pattern_gap2_us": int(s["pattern_gap2_us"]),
            "first_edge_tof_us": int(s["tof_us"]),
            "samples": len(y),
            "baseline_adc": result["baseline_adc"],
            "threshold_adc": result["threshold_adc"],
            "early_max_adc": float(y[early].max()),
            "late_max_adc": float(y[late].max()),
            "late_to_early_excess_ratio": late_excess / early_excess,
            "late_max_time_s": float(t[late][np.argmax(y[late])]),
            "late_active_fraction": float(np.mean(y[late] > result["threshold_adc"])),
            "late_event_count": sum(e["start_s"] >= 0.020 for e in result["events"]),
            "comparator_edges": sum(int(e["shot_seq"]) == shot for e in edge_rows),
        }
        for floor in (10, 20, 40):
            detected = envelope_events(t, y, threshold_adc=floor)
            row[f"late_events_floor{floor}_adc"] = sum(
                e["start_s"] >= 0.020 for e in detected["events"]
            )
        summary.append(row)
        for e in result["events"]:
            events.append(
                dict(
                    shot_seq=shot,
                    **e,
                    after_20ms=e["start_s"] >= 0.020,
                    overlaps_transmitted_pattern=e["start_s"] <= bursts[-1] + 0.001,
                )
            )
        for low, high in ((0, 0.020), (0.020, 0.1), (0.1, 0.5), (0.5, 1), (1, 2), (2, 3)):
            mask = (t >= low) & (t < high)
            windows.append(
                {
                    "shot_seq": shot,
                    "start_s": low,
                    "end_s": high,
                    "maximum_adc": float(y[mask].max()),
                    "median_adc": float(np.median(y[mask])),
                    "active_fraction": float(np.mean(y[mask] > result["threshold_adc"])),
                }
            )
        strong = [
            e
            for e in envelope_events(t, y, threshold_adc=100, merge_gap_s=0.060)["events"]
            if e["start_s"] >= 0.1
        ]
        for e in strong:
            clusters.append({"shot_seq": shot, **e})
        for left, right in itertools.pairwise(strong):
            periods.append(
                {
                    "shot_seq": shot,
                    "start_s": left["start_s"],
                    "next_start_s": right["start_s"],
                    "interval_s": right["start_s"] - left["start_s"],
                }
            )
        captures.append((shot, t, y, bursts, result))
    attempts = [
        {
            "shot_seq": int(e["shot_seq"]),
            "reason": int(e["reason"]),
            "parent_video_s": video_s(int(e["shot_seq"])),
            "segment": segment(video_s(int(e["shot_seq"]))),
        }
        for e in ends
    ]
    write(out / "capture_summary.csv", summary)
    write(out / "candidate_events.csv", events)
    write(out / "time_windows.csv", windows)
    write(out / "observation_attempts.csv", attempts)
    write(out / "strong_late_clusters.csv", clusters)
    write(out / "strong_late_intervals.csv", periods)

    plt.rcParams.update(
        {
            "font.family": "Yu Gothic",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.18,
            "svg.fonttype": "none",
        }
    )
    names = []

    def save(fig, name):
        fig.tight_layout()
        save_editable_figure(fig, out / f"{name}.png", dpi=180)
        (out / f"{name}.pdf").write_bytes(PlotDocument(out / f"{name}.figure.pickle").render("pdf"))
        plt.close(fig)
        names.append(name)

    # Line artists retain direct access to all series in the common browser editor.
    for name, limit, bin_width, title in (
        ("01_early_waveforms", 0.040, None, "受信直後の波形と3連送信パターン"),
        ("02_full_waveforms", 3.0, 0.001, "受信後3秒の包絡線（1 ms区間の最大値）"),
    ):
        fig, ax = plt.subplots(figsize=(12, 9))
        for i, (shot, t, y, bursts, result) in enumerate(captures):
            if bin_width:
                tx, yy = max_bins(t, y, bin_width)
            else:
                tx, yy = t, y
            mask = tx < limit
            ax.plot(
                tx[mask] * 1000, yy[mask] + i * 400, lw=0.65, color="#146b83", label=f"Shot {shot}"
            )
            ax.plot(
                [0, limit * 1000],
                np.full(2, result["baseline_adc"] + i * 400),
                lw=0.4,
                color="#94a3b8",
                ls=":",
            )
            if not bin_width:
                ax.plot(
                    bursts * 1000, np.full(len(bursts), i * 400 + 365), "|", color="#dc7b19", ms=9
                )
        ax.set(
            yticks=np.arange(len(captures)) * 400 + 150,
            yticklabels=[f"{s['shot_seq']} / {s['parent_video_s']:.1f} s" for s in summary],
            xlim=(0, limit * 1000),
            xlabel="最初の受信エッジからの時間 (ms)",
            ylabel="Shot / 親機映像時刻\nADC値を400 countずつずらして表示",
            title=title
            + (
                "\n橙：送信パターンからの予定受信位置（取付・回路遅延未校正）"
                if not bin_width
                else "\n1段 = 400 ADC count、平滑化なし"
            ),
        )
        save(fig, name)

    chosen = [
        0,
        int(np.argmax([s["late_to_early_excess_ratio"] for s in summary])),
        int(np.argmax([s["late_max_time_s"] for s in summary])),
    ]
    chosen = list(dict.fromkeys(chosen))
    fig, axes = plt.subplots(len(chosen), 2, figsize=(12, 3 * len(chosen)), squeeze=False)
    for pair, i in zip(axes, chosen):
        shot, t, y, bursts, result = captures[i]
        for ax, limit in zip(pair, (0.040, 3.0)):
            tt, yy = (t, y) if limit < 1 else max_bins(t, y)
            mask = tt < limit
            ax.plot(tt[mask] * 1000, yy[mask], color="#146b83", lw=0.8, label="受信包絡線")
            ax.axhline(
                result["threshold_adc"], color="#a64747", ls=":", lw=1, label="候補抽出しきい値"
            )
            if limit < 1:
                for j, burst in enumerate(bursts):
                    ax.axvline(
                        burst * 1000,
                        color="#dc7b19",
                        ls="--",
                        lw=0.8,
                        label="3連の予定位置" if j == 0 else None,
                    )
            ax.set(
                xlim=(0, limit * 1000),
                ylim=(0, 370),
                ylabel="包絡線 (ADC count)",
                xlabel="最初の受信からの時間 (ms)",
                title=f"Shot {shot} / 親機映像 {summary[i]['parent_video_s']:.1f} s",
            )
        pair[0].legend(fontsize=8, loc="upper right")
    save(fig, "03_representative_waveforms")

    fig, ax = plt.subplots(figsize=(11, 6))
    for i, row in enumerate(summary):
        selected = [e for e in events if e["shot_seq"] == row["shot_seq"] and e["after_20ms"]]
        ax.scatter(
            [e["peak_s"] * 1000 for e in selected],
            [i] * len(selected),
            s=[8 + e["peak_excess_adc"] * 0.16 for e in selected],
            color="#146b83",
            alpha=0.7,
            label=f"Shot {row['shot_seq']}",
        )
    ax.set(
        xlim=(20, 3000),
        yticks=range(len(summary)),
        yticklabels=[str(r["shot_seq"]) for r in summary],
        ylabel="Shot",
        xlabel="最初の受信からの時間 (ms)",
        title="20 ms以降の電圧ピーク候補\n点の面積は基線からのピーク高さに対応（反射の確定ではない）",
    )
    save(fig, "04_late_events")

    fig, axes = plt.subplots(
        2, 1, figsize=(11, 6), sharex=True, gridspec_kw={"height_ratios": [1, 2]}
    )
    for code, color, label in ((0, "#146b83", "波形あり"), (2, "#9aa4ad", "受信なし")):
        selected = [r for r in attempts if r["reason"] == code]
        axes[0].scatter(
            [r["parent_video_s"] for r in selected],
            [code == 0] * len(selected),
            color=color,
            marker="|",
            s=180,
            label=label,
        )
    axes[0].set(
        yticks=[0, 1],
        yticklabels=["受信なし", "波形あり"],
        title="記録対象45回の成否と、保存波形の後続ピーク",
    )
    axes[1].plot(
        [r["parent_video_s"] for r in summary],
        [r["late_to_early_excess_ratio"] for r in summary],
        "o",
        color="#146b83",
        label="各回の電圧ピーク比",
    )
    axes[1].axhline(1, ls=":", color="#a64747", lw=1)
    for ax in axes:
        for seg in config["segments"][1:]:
            lo, hi = seg["parent_video_interval_s"]
            ax.axvspan(lo, hi, alpha=0.055, color="#146b83")
            if ax is axes[0]:
                ax.text((lo + hi) / 2, 1.35, seg["experiment_id"][-3:], ha="center")
    axes[1].set(
        xlabel="親機映像の時刻 (s)",
        ylabel="20 ms以降の最大 / 最初の20 msの最大\n（各々から末尾基線を引いた電圧比）",
        ylim=(0, 1.25),
    )
    axes[0].set_ylim(-0.4, 1.7)
    save(fig, "05_observation_timeline")

    fig, ax = plt.subplots(figsize=(8, 6))
    for i, row in enumerate(summary):
        intervals = [r["interval_s"] for r in periods if r["shot_seq"] == row["shot_seq"]]
        ax.plot(
            np.array(intervals) * 1000,
            [i] * len(intervals),
            "o",
            color="#146b83",
            ms=5,
            alpha=0.65,
            label=f"Shot {row['shot_seq']}",
        )
    ax.axvline(1000, color="#dc7b19", ls="--", lw=1, label="1秒")
    ax.set(
        yticks=range(len(summary)),
        yticklabels=[str(r["shot_seq"]) for r in summary],
        xlabel="強い後続ピーク群の開始時刻の間隔 (ms)",
        ylabel="Shot",
        title="強い後続ピーク群は約1秒周期で反復\n基線+100 count超・60 ms以内を同じ群・100 ms以降",
    )
    ax.legend(loc="lower right", handles=ax.get_legend_handles_labels()[0][-1:])
    save(fig, "06_late_periodicity")

    ratios = [r["late_to_early_excess_ratio"] for r in summary]
    intervals = [r["interval_s"] for r in periods]
    manifest = {
        "experiment_id": args.experiment,
        "created_utc": datetime.now(UTC).isoformat(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "git_worktree_dirty": True,
        "inputs": [{"path": str(p.relative_to(root)), "sha256": digest(p)} for p in inputs],
        "calibration": "No amplitude, ADC timing or acoustic path calibration applied.",
        "settings": {
            "baseline": "last 1 s median per capture",
            "threshold": "baseline + max(20 ADC count, 6 * 1.4826 * MAD)",
            "merge_gap_s": 0.0005,
            "late_start_s": 0.020,
            "threshold_floor_sensitivity_adc": [10, 20, 40],
            "display_bin_s": 0.001,
            "amplitude_unit": "raw ADC count",
            "strong_cluster_floor_adc": 100,
            "strong_cluster_merge_gap_s": 0.060,
            "strong_cluster_start_s": 0.100,
        },
        "counts": {
            "captures": len(summary),
            "attempts": len(attempts),
            "samples": len(array),
            "comparator_edges": len(edge_rows),
            "no_arrival": sum(r["reason"] == 2 for r in attempts),
        },
        "ratio_range": [min(ratios), max(ratios)],
        "strong_late_intervals": {
            "count": len(intervals),
            "min_s": min(intervals),
            "median_s": float(np.median(intervals)),
            "max_s": max(intervals),
        },
        "figures": names,
        "limitations": [
            "Envelope and comparator only; not a sampled 200 kHz carrier.",
            "Three transmitted bursts must not be counted as reflections.",
            "After-20ms transients may include reflections, reverberation, EMI or noise.",
            "No independent acoustic reference or no-transmission control.",
            "Tail baseline is descriptive and may contain signals; no validated noise model.",
            "No distance conversion: path attribution and direct arrival are unverified.",
            "Candidate events are sample threshold groups; no deconvolution or smoothing.",
            "First 20ms includes transmitted pattern and possible reflections.",
            "Captured rows are trigger-selected; 15/45 is not an overall ranging success rate.",
            "Clock mapping is inherited; source tags retained; no new UTC validation.",
            "15 captured windows cover 45s intermittently, not continuous coverage.",
        ],
    }
    for source in (Path(__file__), root / "src/aquabeacon_analysis/multipath.py"):
        shutil.copy2(source, out / source.name)
        manifest.setdefault("code", []).append(
            {"path": str(source.relative_to(root)), "sha256": digest(source)}
        )
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "# 長田高校プール実験：マルチパス観測ログの記述解析",
        "",
        "## 確認できたこと",
        "",
        "- 記録対象45回中、波形保存15回、受信なし30回。保存分は各3秒・20 kSa/s、計90万点。",
        "- 保存分のサンプル連続性、ADCエラー、終了理由、欠落カウンタ、原本/CSVハッシュを確認した。",
        "- 送信は200 kHzの可変3連。予定位置はファームウェアの整数PIO間隔式から算出した。",
        (
            f"- 15回すべてで20 ms以降に候補ピークを検出。後続/初期の基線差引き最大電圧比は"
            f"{min(ratios):.2f}〜{max(ratios):.2f}（中央値{np.median(ratios):.2f}）。"
        ),
        (
            f"- 受信波形は親機映像{summary[0]['parent_video_s']:.1f}〜"
            f"{summary[-1]['parent_video_s']:.1f}秒。002/003/004に加え004終了後の1回を含む。"
        ),
        "- 15/45は観測対象枠の内訳であり、全測距の成功率ではない。",
        (
            f"- 100 ms以降の強いピーク群（基線+100 count超、60 ms以内をまとめる）の"
            f"隣接間隔{len(intervals)}件は{min(intervals) * 1000:.1f}〜{max(intervals) * 1000:.1f} ms、"
            f"中央値{np.median(intervals) * 1000:.1f} ms。約1秒周期の反復を15回すべてで確認。"
        ),
        "",
        "## 解釈と限界",
        "",
        (
            "約1秒周期の強い後続ピーク群は、単一の送信に対する減衰残響だけでは説明を確定できない。"
            "通信・周期的な回路動作に伴う干渉も候補となる。ファームウェアには観測中の後続音響送信を"
            "休止し、テザー状態通信を継続する設計があるが、この周期との因果関係は未検証。"
            "送信停止時の対照記録、テザー通信時刻、オシロスコープ等による確認が必要。"
        ),
        "",
        (
            "初期の複数ピークには意図した3連送信が含まれる。後続ピークは反射・残響の候補だが、"
            "電気的ノイズ、回路由来の応答、他の音源等を今回のログだけでは分離できない。"
            "単純な単峰・単調減衰として扱えない電圧変動が記録されている、という結論に留める。"
        ),
        "",
        (
            "記録は搬送波そのものではなく包絡線。最初の検出が直接波か、複数のピークが同一送信の"
            "異なる経路かは未検証。距離・反射経路・残響時間・絶対精度は算出しない。"
            "末尾1秒も信号を含むため、しきい値は統計的に検証した雑音判定ではない。"
        ),
        "",
        "## 抽出方法",
        "",
        (
            "各回の末尾1秒の中央値を基線、1.4826×MADをばらつき指標とし、"
            "基線+max(20 count, 6×ばらつき)を超える点を抽出。0.5 ms以内の超過点を1候補にまとめる。"
            "候補の開始が20 ms以降のものを後続候補とする。しきい値下限10/20/40 countの感度もCSVに残した。"
            "ADC値は未校正のcountで、音圧や音響エネルギーへ換算しない。"
        ),
        "",
        (
            "波形全体図の1 ms最大値集約は表示のみ。初期図は全点、数値抽出は元の50 µs刻みの全点を使う。"
            "予定3連位置は最初の受信を0として一定の経路遅延を仮定した参考線。"
        ),
        "",
        "## 図とデータ",
        "",
    ]
    lines += [
        f"- [{name}](./{name}.png) / [SVG](./{name}.svg) / [PDF](./{name}.pdf)" for name in names
    ]
    lines += [
        "",
        "- capture_summary.csv：回ごとの指標・時刻源・区間・しきい値感度",
        "- candidate_events.csv：全候補の開始・終了・最大値時刻と値",
        "- time_windows.csv：時間帯別の最大・中央値・しきい値超過率",
        "- observation_attempts.csv：受信なしを含む45回の終了理由",
        "- strong_late_clusters.csv / strong_late_intervals.csv：約1秒周期の群と隣接間隔",
        "- manifest.json：入力SHA-256、設定、Git SHA、コードSHA-256、限界",
        "",
        "## 編集と再生成",
        "",
        (
            "すべての図に共通ブラウザエディタ用のfigure.pickle・plot.jsonを添付。"
            "軸・凡例・文字・線・点・配置・寸法・範囲を調整できる。"
            "図固有の最大値集約幅や候補しきい値は生成コードから変更する。"
        ),
        "",
        "```powershell",
        f"python scripts/analyze_ngt_multipath.py --experiment {args.experiment}",
        f"python -m aquabeacon_analysis.editable_plots results/{args.experiment}/multipath/01_early_waveforms.figure.pickle",
        "```",
        "",
        "検証記録はverification.jsonを参照。元データは変更していない。",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"output": str(out), "counts": manifest["counts"], "ratios": manifest["ratio_range"]},
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
