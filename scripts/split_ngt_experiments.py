"""Write synchronized sensor/video subsets; originals are never altered."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from bisect import bisect_right
from datetime import UTC, datetime
from pathlib import Path

from aquabeacon_analysis.experiments import load_experiment


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def anchor_map(path):
    records = []
    for row in read_rows(path):
        a, e, end = (int(row[k]) for k in
                     ("utc_anchor_mono_us", "utc_anchor_epoch_us", "utc_valid_until_us"))
        if e > 1_000_000_000_000_000 and end > a:
            records.append((a, e, end))
    records.sort()
    return [r[0] for r in records], records


def anchored_utc(value, anchors):
    times, records = anchors
    index = bisect_right(times, value) - 1
    if index < 0:
        return None
    mono, epoch, end = records[index]
    return epoch + value - mono if mono <= value < end else None


def run_owned(command, journal, purpose, timeout=300):
    record = {"command": command, "purpose": purpose,
              "started_utc": datetime.now(UTC).isoformat()}
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace") as process:
        record["pid"] = process.pid
        print(json.dumps({"pid": process.pid, "purpose": purpose}), flush=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            record["timed_out"] = True
            raise
        finally:
            record["ended_utc"] = datetime.now(UTC).isoformat()
            record["returncode"] = process.returncode
            journal.append(record)
        if process.returncode:
            raise RuntimeError(f"{purpose}: {stderr[-3000:]}")
    return stdout, stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--skip-video", action="store_true")
    parser.add_argument("--reuse-sensor-subsets", action="store_true",
                        help="Reuse completed CSV subsets while finishing video cuts")
    args = parser.parse_args()
    exp = load_experiment(Path.cwd(), args.experiment)
    config = json.loads((Path.cwd() / exp.metadata["config_paths"][0]).read_text())
    sync_path = exp.stage_path("results") / "synchronization/manifest.json"
    sync = json.loads(sync_path.read_text())["cameras"]
    p_clock = sync["parent"]
    p0, scale = p_clock["utc_at_video_zero_s"], p_clock["time_scale"]
    decoded = exp.stage_path("interim") / "decoded"
    anchors = {role: anchor_map(decoded / role /
                               f'{"P" if role == "parent" else "C"}_MAIN_HEALTH_RANGE_'
                               f'{"11071" if role == "parent" else "83304"}.health.csv')
               for role in ("parent", "child")}
    shot_times = {}
    for row in read_rows(exp.stage_path("results") / "ingest/child_shot_utc_mapping.csv"):
        if row["child_tx_utc_us"]:
            shot_times[int(row["shot_seq"])] = int(row["child_tx_utc_us"])
    segments = []
    for spec in config["segments"]:
        item = load_experiment(Path.cwd(), spec["experiment_id"])
        start, end = spec["parent_video_interval_s"]
        out = item.stage_path("interim") / "synchronized"
        out.mkdir(parents=True, exist_ok=True)
        segments.append({**spec, "directory": out, "start_us": round((p0 + scale * start) * 1e6),
                         "end_us": round((p0 + scale * end) * 1e6),
                         "sensor_files": [], "videos": []})
    sources = [("camera", role, exp.stage_path("interim") / "cameras" /
                f"{role}_telemetry.csv") for role in ("parent", "child")]
    for role in ("parent", "child", "spresence"):
        for file in sorted((decoded / role).glob("*.csv")):
            # Full-session source only; calibration/preparatory captures remain in ingest.
            if role == "child" and "83304" not in file.name:
                continue
            if role == "parent" and "11071" not in file.name:
                continue
            sources.append(("sensor", role, file))
    cache_path = exp.stage_path("results") / "synchronization/split_sensor_inventory.json"
    if args.reuse_sensor_subsets:
        cache = json.loads(cache_path.read_text())
        for segment in segments:
            segment["sensor_files"] = cache[segment["experiment_id"]]
        sources = []
    for kind, role, source in sources:
        handles, writers, counts = [], [], [0] * len(segments)
        total, unmapped = 0, 0
        try:
            with source.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fields = list(reader.fieldnames)
                prefix = ["sync_utc_us", "experiment_time_s", "parent_video_time_s",
                          "synchronization_basis"]
                for segment in segments:
                    destination = segment["directory"] / role / source.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    out_handle = destination.open("w", newline="", encoding="utf-8")
                    handles.append(out_handle)
                    writer = csv.DictWriter(out_handle, fieldnames=prefix + fields)
                    writer.writeheader()
                    writers.append(writer)
                for row in reader:
                    total += 1
                    utc, basis = None, "unmapped"
                    if kind == "camera":
                        clock = sync[role]
                        utc = round((clock["utc_at_video_zero_s"] + clock["time_scale"] *
                                     float(row["video_time_s"])) * 1e6)
                        basis = "camera_imu_affine_clock"
                    else:
                        for key in ("sample_utc_us", "utc_us", "utc_read_us"):
                            if row.get(key) and int(row[key]) > 1_000_000_000_000_000:
                                utc, basis = int(row[key]), f"recorded_{key}"
                                break
                        if utc is None and row.get("utc_reference_ms"):
                            candidate = int(row["utc_reference_ms"]) * 1000
                            if candidate > 1_000_000_000_000_000:
                                utc, basis = candidate, "recorded_utc_reference_ms"
                        if utc is None and row.get("utc_s") and int(row["utc_s"]) > 1_000_000_000:
                            utc = int(row["utc_s"]) * 1_000_000 + int(row.get("nav_usec", 0))
                            basis = "recorded_utc_s_nav_usec"
                        if utc is None and role in anchors and row.get("mono_us"):
                            utc = anchored_utc(int(row["mono_us"]), anchors[role])
                            basis = "valid_health_clock_anchor"
                        if source.name.endswith((".shot.csv", ".range_measurement.csv")):
                            utc = shot_times.get(int(row["shot_seq"]))
                            basis = "child_acoustic_transmit_edge_utc"
                    if utc is None:
                        unmapped += 1
                        continue
                    for index, segment in enumerate(segments):
                        if segment["start_us"] <= utc < segment["end_us"]:
                            writers[index].writerow({"sync_utc_us": utc,
                                "experiment_time_s": f'{(utc - segment["start_us"]) / 1e6:.6f}',
                                "parent_video_time_s": f"{(utc / 1e6 - p0) / scale:.6f}",
                                "synchronization_basis": basis, **row})
                            counts[index] += 1
        finally:
            for handle in handles:
                handle.close()
        for segment, count in zip(segments, counts, strict=True):
            segment["sensor_files"].append({"source": str(source),
                "output": str(segment["directory"] / role / source.name),
                "selected_rows": count, "source_rows": total, "unmapped_source_rows": unmapped})
        print(f"Split {source.name}: {counts}; unclocked {unmapped}/{total}", flush=True)
    cache_path.write_text(json.dumps({s["experiment_id"]: s["sensor_files"] for s in segments},
                                    indent=2), encoding="utf-8")
    journal = []
    for segment in segments:
        for role in ("parent", "child"):
            clock = sync[role]
            video = Path(clock["video"])
            requested = [(segment[key] / 1e6 - clock["utc_at_video_zero_s"]) /
                         clock["time_scale"] for key in ("start_us", "end_us")]
            start = max(0., requested[0])
            end = min(requested[1], 2049.047 if role == "parent" else 1530.595733)
            destination = segment["directory"] / role / f"{role}_video.mp4"
            info = {"source": str(video), "output": str(destination),
                    "requested_source_video_s": requested, "available_source_video_s": [start, end],
                    "missing_leading_s": max(0., -requested[0]),
                    "method": "stream copy; source frame timestamps; MP4 edit-list preroll",
                    "telemetry": "DJI metadata retained in separate synchronized telemetry CSV",
                    "retiming": "Playback rate is unchanged; affine clock stored in manifest"}
            if not args.skip_video and end > start:
                command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                           "-threads", "2", "-ss", f"{start:.9f}", "-i", str(video),
                           "-t", f"{end - start:.9f}", "-map", "0:v:0", "-map", "0:a:0?",
                           "-c", "copy", "-movflags", "+faststart", str(destination)]
                run_owned(command, journal, f'{segment["experiment_id"]} {role} video')
                probe, _ = run_owned(["ffprobe", "-v", "error", "-show_entries",
                                     "format=duration:stream=index,codec_type,r_frame_rate,start_time,duration",
                                     "-of", "json", str(destination)], journal, "verify clip", 30)
                info["probe"] = json.loads(probe)
                # MP4 container duration includes hidden decoder preroll on stream-copy seeks.
                # The edit-list-adjusted video stream duration describes displayed frames.
                stream = next(row for row in info["probe"]["streams"]
                              if row["codec_type"] == "video")
                duration = float(stream["duration"])
                info["duration_error_s"] = duration - (end - start)
                if abs(info["duration_error_s"]) > .15:
                    raise ValueError(f"Clip duration mismatch: {destination}")
            segment["videos"].append(info)
        report_dir = Path("results") / segment["experiment_id"] / "synchronization"
        report_dir.mkdir(parents=True, exist_ok=True)
        record = {**segment, "directory": str(segment["directory"]),
                  "clock_manifest": str(sync_path), "interval_policy": "half-open [start,end)",
                  "start_utc": datetime.fromtimestamp(segment["start_us"] / 1e6, UTC).isoformat(),
                  "end_utc": datetime.fromtimestamp(segment["end_us"] / 1e6, UTC).isoformat(),
                  "processes": journal.copy()}
        (report_dir / "manifest.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        (report_dir / "report.md").write_text(
            f'# {segment["experiment_id"]} 同期・切出し\n\n'
            f'親機原動画の {segment["parent_video_interval_s"]} 秒。'
            f'UTC {record["start_utc"]} から {record["end_utc"]}。\n\n'
            f'同期済みCSV・動画: `{segment["directory"]}`。'
            '原本の値・固有時刻列を保持し、共通UTCと区間内時刻を追加。'
            '区間末尾は含まない。時刻を付与できない行数もmanifestに記録。\n\n'
            '動画は映像・音声の無再圧縮コピー。DJIメタデータは別CSVへ保存。'
            '再生速度は原動画と同じで、時計の微小な進み方の補正はCSVとmanifestに保持。'
            'MP4の編集リストで非表示となるキーフレーム前の復号用データを含み得る。\n',
            encoding="utf-8")
    output = exp.stage_path("results") / "synchronization/split_processes.json"
    output.write_text(json.dumps({"controller_pid": os.getpid(), "processes": journal}, indent=2))


if __name__ == "__main__":
    main()
