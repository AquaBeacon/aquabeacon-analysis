from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt


@dataclass(frozen=True)
class CorrelationResult:
    pair: str
    body_source: str
    camera_source: str
    offset_s: float
    peak_correlation: float
    peak_prominence: float
    overlap_s: float
    samples: int
    status: str
    drift_s_per_hour: float | None
    window_offsets_s: list[float]
    window_correlations: list[float]


def _read_columns(path: Path, names: list[str]) -> dict[str, np.ndarray]:
    values = {name: [] for name in names}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in names if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"missing columns in {path}: {missing}")
        for row in reader:
            for name in names:
                try:
                    values[name].append(float(row[name]))
                except (TypeError, ValueError):
                    values[name].append(float("nan"))
    return {name: np.asarray(column, dtype=np.float64) for name, column in values.items()}


def _acceleration_signal(path: Path, camera: bool = False) -> tuple[np.ndarray, np.ndarray]:
    axes = ["accel_x_g", "accel_y_g", "accel_z_g"] if camera else ["ax", "ay", "az"]
    data = _read_columns(path, ["experiment_time_s", *axes])
    acceleration = np.sqrt(sum(data[axis] ** 2 for axis in axes))
    valid = np.isfinite(data["experiment_time_s"]) & np.isfinite(acceleration)
    return data["experiment_time_s"][valid], acceleration[valid]


def _moving_average(values: np.ndarray, width: int) -> np.ndarray:
    if width <= 1:
        return values.copy()
    kernel = np.ones(width, dtype=np.float64) / width
    return np.convolve(values, kernel, mode="same")


def _prepare_signal(
    times: np.ndarray,
    values: np.ndarray,
    grid: np.ndarray,
    sample_rate_hz: float,
) -> np.ndarray:
    interpolated = np.interp(grid, times, values)
    high_pass = interpolated - _moving_average(interpolated, max(3, round(sample_rate_hz)))
    median = np.median(high_pass)
    mad = np.median(np.abs(high_pass - median))
    scale = max(1.4826 * mad, float(np.std(high_pass)), 1e-12)
    return (high_pass - median) / scale


def estimate_time_offset(
    body_times: np.ndarray,
    body_values: np.ndarray,
    camera_times: np.ndarray,
    camera_values: np.ndarray,
    *,
    max_lag_s: float = 5.0,
    sample_rate_hz: float = 30.0,
) -> tuple[float, float, float, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    start = max(float(body_times[0]), float(camera_times[0])) + max_lag_s
    end = min(float(body_times[-1]), float(camera_times[-1])) - max_lag_s
    if end - start < 10:
        raise ValueError("less than 10 seconds remain after lag margins")
    grid = np.arange(start, end, 1 / sample_rate_hz)
    body = _prepare_signal(body_times, body_values, grid, sample_rate_hz)
    lag_steps = round(max_lag_s * sample_rate_hz)
    offsets = np.arange(-lag_steps, lag_steps + 1, dtype=np.float64) / sample_rate_hz
    correlations = np.empty(offsets.size, dtype=np.float64)
    for index, offset in enumerate(offsets):
        shifted = _prepare_signal(
            camera_times + offset,
            camera_values,
            grid,
            sample_rate_hz,
        )
        correlations[index] = np.corrcoef(body, shifted)[0, 1]
    best = int(np.nanargmax(correlations))
    offset = offsets[best]
    if 0 < best < offsets.size - 1:
        left, center, right = correlations[best - 1 : best + 2]
        denominator = left - 2 * center + right
        if abs(denominator) > 1e-12:
            offset += 0.5 * (left - right) / denominator / sample_rate_hz
    exclusion = max(1, round(0.5 * sample_rate_hz))
    competitor = np.concatenate(
        (correlations[: max(0, best - exclusion)], correlations[best + exclusion + 1 :])
    )
    second = float(np.nanmax(competitor)) if competitor.size else -1.0
    prominence = float(correlations[best] - second)
    shifted_camera = _prepare_signal(
        camera_times + offset,
        camera_values,
        grid,
        sample_rate_hz,
    )
    return (
        float(offset),
        float(correlations[best]),
        prominence,
        offsets,
        correlations,
        body,
        shifted_camera,
    )


def _window_offsets(
    body_times: np.ndarray,
    body_values: np.ndarray,
    camera_times: np.ndarray,
    camera_values: np.ndarray,
) -> tuple[list[float], list[float], float | None]:
    start = max(float(body_times[0]), float(camera_times[0]))
    end = min(float(body_times[-1]), float(camera_times[-1]))
    duration = end - start
    if duration < 90:
        return [], [], None
    centers = np.linspace(start + duration / 6, end - duration / 6, 3)
    width = min(120.0, duration / 3)
    offsets = []
    correlations = []
    used_centers = []
    for center in centers:
        lower, upper = center - width / 2, center + width / 2
        body_mask = (body_times >= lower) & (body_times <= upper)
        camera_mask = (camera_times >= lower) & (camera_times <= upper)
        try:
            result = estimate_time_offset(
                body_times[body_mask],
                body_values[body_mask],
                camera_times[camera_mask],
                camera_values[camera_mask],
                max_lag_s=3.0,
            )
        except (ValueError, IndexError):
            continue
        offsets.append(result[0])
        correlations.append(result[1])
        used_centers.append(center)
    reliable = [
        (center, offset, correlation)
        for center, offset, correlation in zip(
            used_centers, offsets, correlations, strict=True
        )
        if correlation >= 0.35
    ]
    if len(reliable) < 3:
        return offsets, correlations, None
    median_offset = float(np.median([item[1] for item in reliable]))
    consensus = [item for item in reliable if abs(item[1] - median_offset) <= 0.1]
    if len(consensus) < 3:
        return offsets, correlations, None
    slope = np.polyfit(
        np.asarray([item[0] for item in consensus]),
        np.asarray([item[1] for item in consensus]),
        1,
    )[0]
    return offsets, correlations, float(slope * 3600)


def _analyze_pair(pair: str, body_path: Path, camera_path: Path) -> tuple[CorrelationResult, dict]:
    body_times, body_values = _acceleration_signal(body_path)
    camera_times, camera_values = _acceleration_signal(camera_path, camera=True)
    result = estimate_time_offset(body_times, body_values, camera_times, camera_values)
    offset, peak, prominence, lags, correlations, body, camera = result
    window_offsets, window_correlations, drift = _window_offsets(
        body_times, body_values, camera_times, camera_values
    )
    reliable_offsets = [
        offset_value
        for offset_value, correlation in zip(
            window_offsets, window_correlations, strict=True
        )
        if correlation >= 0.35
    ]
    consensus = []
    if len(reliable_offsets) >= 2:
        median_offset = float(np.median(reliable_offsets))
        consensus = [
            value for value in reliable_offsets if abs(value - median_offset) <= 0.1
        ]
    if len(consensus) >= 2:
        offset = float(np.median(consensus))
        status = "applied_window_consensus"
    elif peak >= 0.25 and prominence >= 0.08:
        status = "applied_global_peak"
    else:
        status = "warning_ambiguous"
    overlap = min(body_times[-1], camera_times[-1]) - max(body_times[0], camera_times[0])
    record = CorrelationResult(
        pair=pair,
        body_source=str(body_path),
        camera_source=str(camera_path),
        offset_s=offset,
        peak_correlation=peak,
        peak_prominence=prominence,
        overlap_s=float(overlap),
        samples=int(body.size),
        status=status,
        drift_s_per_hour=drift,
        window_offsets_s=window_offsets,
        window_correlations=window_correlations,
    )
    diagnostic = {
        "lags": lags,
        "correlations": correlations,
        "body": body,
        "camera": camera,
        "grid_start_s": max(float(body_times[0]), float(camera_times[0])) + 5.0,
        "sample_rate_hz": 30.0,
    }
    return record, diagnostic


def _write_refined_camera(source: Path, output: Path, offset_s: float, applied: bool) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    offset_us = round(offset_s * 1_000_000) if applied else 0
    with source.open("r", newline="", encoding="utf-8-sig") as input_handle:
        reader = csv.DictReader(input_handle)
        fields = [
            "refined_sync_utc_us",
            "refined_experiment_time_s",
            "correlation_offset_applied_s",
            "correlation_status",
            *(reader.fieldnames or []),
        ]
        with output.open("w", newline="", encoding="utf-8") as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=fields)
            writer.writeheader()
            for row in reader:
                utc_us = int(row["sync_utc_us"]) + offset_us
                experiment_s = float(row["experiment_time_s"]) + offset_us / 1e6
                writer.writerow(
                    {
                        "refined_sync_utc_us": utc_us,
                        "refined_experiment_time_s": f"{experiment_s:.6f}",
                        "correlation_offset_applied_s": f"{offset_us / 1e6:.6f}",
                        "correlation_status": "applied" if applied else "not_applied_ambiguous",
                        **row,
                    }
                )
                count += 1
    return count


def _quality_for_csv(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    row_count = 0
    timestamps = []
    blank_cells = 0
    nonfinite_cells = 0
    total_cells = 0
    column_blanks: dict[str, int] = {}
    column_nonfinite: dict[str, int] = {}
    sequence_values = []
    sequence_field = ""
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        column_blanks = dict.fromkeys(fields, 0)
        column_nonfinite = dict.fromkeys(fields, 0)
        time_field = "sync_utc_us" if "sync_utc_us" in fields else None
        sequence_field = next(
            (name for name in ("seq", "index", "camera_frame") if name in fields), ""
        )
        for row in reader:
            row_count += 1
            if time_field and row[time_field]:
                timestamps.append(int(row[time_field]))
            if sequence_field and row[sequence_field] not in (None, ""):
                try:
                    sequence_values.append(int(float(row[sequence_field])))
                except ValueError:
                    pass
            for name, value in row.items():
                total_cells += 1
                if value is None or value == "":
                    blank_cells += 1
                    column_blanks[name] += 1
                    continue
                normalized = value.strip().lower()
                if normalized in {
                    "nan",
                    "+nan",
                    "-nan",
                    "inf",
                    "+inf",
                    "-inf",
                    "infinity",
                    "+infinity",
                    "-infinity",
                }:
                    nonfinite_cells += 1
                    column_nonfinite[name] += 1
    timestamp_array = np.asarray(timestamps, dtype=np.int64)
    deltas = np.diff(timestamp_array) / 1e6 if timestamp_array.size > 1 else np.array([])
    positive = deltas[deltas > 0]
    median = float(np.median(positive)) if positive.size else float("nan")
    gap_threshold = median * 3 if math.isfinite(median) else float("nan")
    sequence_deltas = np.diff(np.asarray(sequence_values, dtype=np.int64))
    summary = {
        "file": str(path),
        "rows": row_count,
        "columns": len(fields),
        "start_utc_us": int(timestamp_array[0]) if timestamp_array.size else "",
        "end_utc_us": int(timestamp_array[-1]) if timestamp_array.size else "",
        "duration_s": (
            float((timestamp_array[-1] - timestamp_array[0]) / 1e6)
            if timestamp_array.size > 1
            else 0.0
        ),
        "median_interval_s": median,
        "mean_interval_s": float(np.mean(positive)) if positive.size else float("nan"),
        "std_interval_s": float(np.std(positive)) if positive.size else float("nan"),
        "min_interval_s": float(np.min(positive)) if positive.size else float("nan"),
        "max_interval_s": float(np.max(positive)) if positive.size else float("nan"),
        "duplicate_timestamps": int(np.sum(deltas == 0)),
        "backward_timestamps": int(np.sum(deltas < 0)),
        "largest_backward_s": (
            float(abs(np.min(deltas[deltas < 0]))) if np.any(deltas < 0) else 0.0
        ),
        "large_gaps": int(np.sum(deltas > gap_threshold)) if math.isfinite(gap_threshold) else 0,
        "blank_cells": blank_cells,
        "blank_fraction": blank_cells / total_cells if total_cells else 0.0,
        "nonfinite_numeric_cells": nonfinite_cells,
        "sequence_field": sequence_field,
        "sequence_gaps": int(np.sum(sequence_deltas > 1)),
        "sequence_missing_rows": int(np.sum(sequence_deltas[sequence_deltas > 1] - 1)),
        "sequence_non_increasing": int(np.sum(sequence_deltas <= 0)),
    }
    columns = [
        {
            "file": str(path),
            "column": name,
            "rows": row_count,
            "blank_cells": column_blanks[name],
            "blank_fraction": column_blanks[name] / row_count if row_count else 0.0,
            "nonfinite_numeric_cells": column_nonfinite[name],
        }
        for name in fields
        if column_blanks[name] or column_nonfinite[name]
    ]
    return summary, columns


def _write_dict_csv(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = []
    for record in records:
        for key in record:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def _plot_timeline(quality: list[dict], output: Path, title: str) -> None:
    rows = [row for row in quality if row["start_utc_us"] != ""]
    if not rows:
        return
    origin = min(int(row["start_utc_us"]) for row in rows)
    fig, axis = plt.subplots(figsize=(12, max(4.5, 0.48 * len(rows) + 1.5)))
    for index, row in enumerate(rows):
        start = (int(row["start_utc_us"]) - origin) / 1e6
        end = (int(row["end_utc_us"]) - origin) / 1e6
        axis.barh(index, end - start, left=start, height=0.55, color="#277DA1")
    axis.set_yticks(range(len(rows)), [Path(str(row["file"])).name for row in rows])
    axis.set_xlabel("Seconds from earliest synchronized sample")
    axis.set_title(title)
    axis.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output, dpi=170)
    plt.close(fig)


def _plot_sampling(quality: list[dict], output: Path, title: str) -> None:
    labels = [Path(str(row["file"])).name for row in quality]
    medians = [float(row["median_interval_s"]) for row in quality]
    maxima = [float(row["max_interval_s"]) for row in quality]
    positions = np.arange(len(labels))
    fig, axis = plt.subplots(figsize=(12, max(5, 0.5 * len(labels) + 1.5)))
    axis.barh(positions, maxima, color="#F9844A", alpha=0.65, label="Maximum")
    axis.barh(positions, medians, color="#43AA8B", label="Median")
    axis.set_yticks(positions, labels)
    axis.set_xscale("log")
    axis.set_xlabel("Sampling interval (s, log scale)")
    axis.set_title(title)
    axis.legend()
    axis.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output, dpi=170)
    plt.close(fig)


def _plot_correlations(
    records: list[CorrelationResult], diagnostics: list[dict], output: Path, title: str
) -> None:
    fig, axes = plt.subplots(len(records), 2, figsize=(14, 4.2 * len(records)), squeeze=False)
    for row, (record, diagnostic) in enumerate(zip(records, diagnostics, strict=True)):
        sample_rate = diagnostic["sample_rate_hz"]
        count = min(len(diagnostic["body"]), round(40 * sample_rate))
        energy = np.convolve(
            diagnostic["body"] ** 2, np.ones(max(1, round(5 * sample_rate))), mode="same"
        )
        center = int(np.argmax(energy))
        lower = max(0, center - count // 2)
        upper = min(len(diagnostic["body"]), lower + count)
        time = diagnostic["grid_start_s"] + np.arange(lower, upper) / sample_rate
        axes[row, 0].plot(time, diagnostic["body"][lower:upper], label="Body IMU", lw=1.1)
        axes[row, 0].plot(
            time,
            diagnostic["camera"][lower:upper],
            label="Camera IMU corrected",
            lw=1.0,
            alpha=0.8,
        )
        axes[row, 0].set_title(f"{record.pair}: strongest-motion window")
        axes[row, 0].set_xlabel("Experiment time (s)")
        axes[row, 0].set_ylabel("High-pass acceleration norm (normalized)")
        axes[row, 0].legend()
        axes[row, 0].grid(alpha=0.2)
        axes[row, 1].plot(diagnostic["lags"], diagnostic["correlations"], color="#D1495B")
        axes[row, 1].axvline(record.offset_s, color="#00798C", ls="--")
        axes[row, 1].set_title(
            f"offset={record.offset_s:+.4f}s, r={record.peak_correlation:.3f}, "
            f"status={record.status}"
        )
        axes[row, 1].set_xlabel("Offset added to camera timestamps (s)")
        axes[row, 1].set_ylabel("Pearson correlation")
        axes[row, 1].grid(alpha=0.2)
    fig.suptitle(title, fontsize=15)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(output, dpi=170)
    plt.close(fig)


def _session_dirs(interim_root: Path, requested: list[str] | None) -> list[Path]:
    directories = sorted(path for path in interim_root.glob("*_cmain_*") if path.is_dir())
    if not requested:
        return directories
    tokens = {token.lower() for token in requested}
    numeric_tokens = {str(int(token)) for token in tokens if token.isdigit()}
    selected = [
        path
        for path in directories
        if path.name.lower() in tokens
        or path.name.rsplit("_", 1)[-1].lstrip("0") in numeric_tokens
    ]
    if not selected:
        raise FileNotFoundError(f"no matching synchronized sessions for {requested}")
    return selected


def analyze_synchronization(
    interim_root: Path = Path("data/interim"),
    results_root: Path = Path("results"),
    sessions: list[str] | None = None,
) -> list[Path]:
    interim_root = interim_root.resolve()
    results_root = results_root.resolve()
    summaries = []
    for session_dir in _session_dirs(interim_root, sessions):
        c_main_imu = next((session_dir / "c_main").glob("C_MAIN_IMU_100Hz_*.csv"))
        sony_imu = next((session_dir / "spresense").glob("I*.CSV"))
        c_camera = next((session_dir / "camera_telemetry").glob("C_MAIN_CAM_*.csv"))
        p_camera = next((session_dir / "camera_telemetry").glob("P_MAIN_CAM_*.csv"))
        pairs = [
            ("C_MAIN body to C_MAIN camera", c_main_imu, c_camera),
            ("SONY on P_MAIN to P_MAIN camera", sony_imu, p_camera),
        ]
        records = []
        diagnostics = []
        for name, body, camera in pairs:
            record, diagnostic = _analyze_pair(name, body, camera)
            records.append(record)
            diagnostics.append(diagnostic)
            refined = (
                session_dir
                / "refined_sync"
                / f"{camera.stem}_refined.csv"
            )
            _write_refined_camera(
                camera,
                refined,
                record.offset_s,
                record.status.startswith("applied"),
            )
        report_dir = results_root / session_dir.name / "synchronization" / "refined"
        report_dir.mkdir(parents=True, exist_ok=True)
        offset_records = []
        for record in records:
            item = asdict(record)
            item["window_offsets_s"] = json.dumps(item["window_offsets_s"])
            item["window_correlations"] = json.dumps(item["window_correlations"])
            offset_records.append(item)
        _write_dict_csv(report_dir / "imu_offsets.csv", offset_records)
        csv_files = sorted(
            [
                *session_dir.glob("c_main/*.csv"),
                *session_dir.glob("spresense/*.CSV"),
                *session_dir.glob("camera_telemetry/*.csv"),
            ]
        )
        quality = []
        column_quality = []
        for path in csv_files:
            file_quality, columns = _quality_for_csv(path)
            quality.append(file_quality)
            column_quality.extend(columns)
        _write_dict_csv(report_dir / "sensor_quality.csv", quality)
        _write_dict_csv(report_dir / "column_quality.csv", column_quality)
        _plot_timeline(quality, report_dir / "timeline.png", f"{session_dir.name} sensor coverage")
        _plot_sampling(
            quality,
            report_dir / "sampling_intervals.png",
            f"{session_dir.name} sampling intervals",
        )
        _plot_correlations(
            records,
            diagnostics,
            report_dir / "sync_diagnostics.png",
            f"{session_dir.name} IMU synchronization",
        )
        warnings = []
        for record in records:
            if not record.status.startswith("applied"):
                warnings.append(
                    f"{record.pair}: ambiguous correlation; offset was not applied "
                    f"(r={record.peak_correlation:.3f}, prominence={record.peak_prominence:.3f})."
                )
            if record.drift_s_per_hour is not None and abs(record.drift_s_per_hour) > 0.05:
                warnings.append(
                    f"{record.pair}: window offsets suggest possible clock drift of "
                    f"{record.drift_s_per_hour:+.3f} s/hour."
                )
        for row in quality:
            if (
                row["backward_timestamps"]
                or row["duplicate_timestamps"]
                or row["nonfinite_numeric_cells"]
                or row["large_gaps"]
                or row["sequence_gaps"]
            ):
                warnings.append(
                    f"{Path(str(row['file'])).name}: backward={row['backward_timestamps']}, "
                    f"largest_backward={row['largest_backward_s']:.6f}s, "
                    f"duplicate={row['duplicate_timestamps']}, large_gaps={row['large_gaps']}, "
                    f"max_interval={row['max_interval_s']:.6f}s, "
                    f"sequence_gaps={row['sequence_gaps']} "
                    f"(missing_rows={row['sequence_missing_rows']}), "
                    f"nonfinite={row['nonfinite_numeric_cells']}."
                )
        summary = {
            "experiment_id": session_dir.name,
            "imu_offsets": [asdict(record) for record in records],
            "quality_files": len(quality),
            "warnings": warnings,
        }
        summary_json = report_dir / "analysis_summary.json"
        summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary_md = report_dir / "report.md"
        summary_md.write_text(_report_markdown(summary), encoding="utf-8")
        summaries.append(summary_md)
        print(
            session_dir.name,
            ", ".join(f"{record.pair}={record.offset_s:+.4f}s ({record.status})" for record in records),
        )
    return summaries


def _report_markdown(summary: dict) -> str:
    lines = [
        f"# Refined synchronization: {summary['experiment_id']}",
        "",
        "## IMU correlation",
        "",
        "| Pair | Offset added to camera | Correlation | Prominence | Status | Drift |",
        "|---|---:|---:|---:|---|---:|",
    ]
    for record in summary["imu_offsets"]:
        drift = (
            "n.a."
            if record["drift_s_per_hour"] is None
            else f"{record['drift_s_per_hour']:+.3f} s/hour"
        )
        lines.append(
            f"| {record['pair']} | {record['offset_s']:+.6f} s | "
            f"{record['peak_correlation']:.3f} | {record['peak_prominence']:.3f} | "
            f"{record['status']} | {drift} |"
        )
    lines.extend(
        [
            "",
            f"Quality checks covered {summary['quality_files']} synchronized CSV files.",
            "",
            "## Warnings",
            "",
        ]
    )
    if summary["warnings"]:
        lines.extend(f"- {warning}" for warning in summary["warnings"])
    else:
        lines.append("- No timestamp-order or non-finite numeric issues were detected.")
    lines.append("")
    return "\n".join(lines)
