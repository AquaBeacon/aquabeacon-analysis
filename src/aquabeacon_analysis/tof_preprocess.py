"""Extract the sustained ToF experiment window from synchronized interim data."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def tof_window(rows: list[dict[str, str]], minimum_run: int = 5) -> tuple[int, int]:
    """Return [start, end) in UTC microseconds; ignore isolated startup readings."""
    valid = [int(row["tof_us"]) > 0 and row["range_state"] in {"2", "3"} for row in rows]
    first = None
    run_start = None
    for index, active in enumerate(valid + [False]):
        if active and run_start is None:
            run_start = index
        if not active and run_start is not None:
            if index - run_start >= minimum_run and first is None:
                first = run_start
            run_start = None
    if first is None:
        raise ValueError(f"no sustained ToF run of {minimum_run} rows")
    last = max(index for index in range(first, len(rows)) if valid[index])
    if last + 1 == len(rows):
        raise ValueError("ToF never becomes inactive before the health log ends")
    return int(rows[first]["sync_utc_us"]), int(rows[last + 1]["sync_utc_us"])


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _iso(utc_us: int) -> str:
    return datetime.fromtimestamp(utc_us / 1_000_000, UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _filter_csv(source: Path, destination: Path, start: int, end: int,
                *, refined: bool = False, health: bool = False) -> dict:
    time_column = "refined_sync_utc_us" if refined else "sync_utc_us"
    with source.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if time_column not in (reader.fieldnames or []):
            raise ValueError(f"{source}: missing {time_column}")
        fields = ["window_time_s", *(["tof_valid"] if health else []), *reader.fieldnames]
        destination.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with destination.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for row in reader:
                utc_us = int(row[time_column])
                if start <= utc_us < end:
                    row["window_time_s"] = f"{(utc_us - start) / 1_000_000:.6f}"
                    if health:
                        row["tof_valid"] = int(
                            int(row["tof_us"]) > 0 and row["range_state"] in {"2", "3"}
                        )
                    writer.writerow(row)
                    count += 1
    return {"source": str(source), "output": str(destination), "rows": count,
            "time_column": time_column}


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _trim_video(source: Path, destination: Path, offset_s: float, duration_s: float,
                *, horizontal: bool) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".part.mp4")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss",
               f"{offset_s:.6f}", "-i", str(source), "-t", f"{duration_s:.6f}",
               "-map", "0:v:0", "-map", "0:a?", "-c", "copy"]
    if horizontal:
        command += ["-metadata:s:v:0", "rotate=0"]
    command += ["-avoid_negative_ts", "make_zero", str(temporary)]
    subprocess.run(command, check=True)
    info = _probe(temporary)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    rotation = next((s.get("rotation") for s in video.get("side_data_list", [])
                     if s.get("side_data_type") == "Display Matrix"), None)
    if horizontal and rotation not in (None, 0):
        temporary.unlink()
        raise RuntimeError(f"rotation metadata remains: {rotation}")
    temporary.replace(destination)
    return {"source": str(source), "output": str(destination),
            "source_offset_s": offset_s, "requested_duration_s": duration_s,
            "output_duration_s": float(info["format"]["duration"]),
            "width": video["width"], "height": video["height"],
            "display_rotation_deg": rotation, "bytes": destination.stat().st_size,
            "cut_method": "stream_copy; leading keyframe precision is not guaranteed"}


def preprocess_experiment(experiment: Path, processed_root: Path,
                          results_root: Path, repository: Path) -> Path:
    health_path = next((experiment / "c_main").glob("*HEALTH_RANGE*.csv"))
    health_rows = _read_csv(health_path)
    start, end = tof_window(health_rows)
    output = processed_root / experiment.name / "tof_window"
    report = results_root / experiment.name / "preprocessing"
    csv_records = []
    for folder in ("c_main", "spresense"):
        for source in sorted(path for path in (experiment / folder).iterdir()
                             if path.suffix.lower() == ".csv"):
            csv_records.append(_filter_csv(source, output / folder / source.name,
                                           start, end, health=source == health_path))
    video_records = []
    for source in sorted((experiment / "refined_sync").glob("*_refined.csv")):
        camera = "C_MAIN" if source.name.startswith("C_MAIN") else "P_MAIN"
        destination = output / "camera_telemetry" / source.name
        csv_records.append(_filter_csv(source, destination, start, end, refined=True))
        with source.open(newline="", encoding="utf-8") as stream:
            first = next(csv.DictReader(stream))
        video_start = int(first["refined_sync_utc_us"])
        offset_s = (start - video_start) / 1_000_000
        duration_s = (end - start) / 1_000_000
        for video_source in sorted((experiment / "camera_video").glob(camera + "*.mp4")):
            source_duration = float(_probe(video_source)["format"]["duration"])
            if offset_s < 0 or offset_s + duration_s > source_duration + 0.1:
                raise ValueError(f"video does not cover ToF window: {video_source}")
            video_records.append(_trim_video(
                video_source, output / "camera_video" / video_source.name,
                offset_s, duration_s, horizontal=camera == "C_MAIN",
            ))
    if len(video_records) != 4:
        raise ValueError(f"expected four camera videos, got {len(video_records)}")
    git = subprocess.run(
        ["git", "-c", f"safe.directory={repository.as_posix()}", "rev-parse", "HEAD"],
        cwd=repository, text=True, capture_output=True, check=False,
    )
    conditions = {}
    for folder in ("config", "calibration"):
        conditions[folder] = {
            str(path.relative_to(repository)): _sha256(path)
            for path in sorted((repository / folder).glob("*")) if path.is_file()
        }
    manifest = {
        "experiment_id": experiment.name,
        "window_start_utc_us": start, "window_end_utc_us_exclusive": end,
        "window_start_utc": _iso(start), "window_end_utc_exclusive": _iso(end),
        "duration_s": (end - start) / 1_000_000,
        "selection": "first run of at least 5 consecutive valid ToF rows to first invalid row after final valid reading",
        "valid_tof_condition": "tof_us > 0 and range_state in {2,3}",
        "camera_clock": "refined_sync_utc_us; video origin from first refined telemetry row",
        "camera_rotation": "C_MAIN display rotation reset to 0 degrees; encoded pixels unchanged",
        "video_cut_limit": "stream-copy trims may start on nearby keyframes; use refined telemetry for exact UTC",
        "git_commit": git.stdout.strip() if git.returncode == 0 else None,
        "input_conditions_sha256": conditions,
        "csv_outputs": csv_records, "video_outputs": video_records,
    }
    report.mkdir(parents=True, exist_ok=True)
    manifest_path = report / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def preprocess_all(interim_root: Path, processed_root: Path, results_root: Path,
                   repository: Path, sessions: list[str] | None = None) -> list[Path]:
    experiments = sorted(p for p in interim_root.iterdir() if p.is_dir())
    if sessions:
        experiments = [p for p in experiments if p.name in sessions or p.name[-3:] in sessions]
    return [preprocess_experiment(p, processed_root, results_root, repository)
            for p in experiments]
