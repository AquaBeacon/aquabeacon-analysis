from __future__ import annotations

import csv
import json
import math
import re
import shutil
import struct
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

CREATION_TIME_RE = re.compile(r"creation_time\s*:\s*(\S+)")
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
SESSION_RE = re.compile(r"_(\d{5})\.csv$", re.IGNORECASE)


@dataclass(frozen=True)
class Session:
    suffix: str
    experiment_id: str
    start_utc_us: int
    end_utc_us: int


@dataclass(frozen=True)
class ClockFit:
    intercept_us: float
    slope: float
    residual_rms_us: float
    points: int

    def utc_us(self, mono_us: int) -> int:
        return round(self.intercept_us + self.slope * mono_us)

    def mono_us(self, utc_us: int) -> int:
        return round((utc_us - self.intercept_us) / self.slope)


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    start_utc_us: int
    duration_s: float

    @property
    def end_utc_us(self) -> int:
        return self.start_utc_us + round(self.duration_s * 1_000_000)


def _iso_utc(utc_us: int) -> str:
    return datetime.fromtimestamp(utc_us / 1_000_000, UTC).isoformat(timespec="microseconds")


def _read_rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        yield from csv.DictReader(handle)


def _first_last_valid_utc(path: Path) -> tuple[int, int] | None:
    first = None
    last = None
    for row in _read_rows(path):
        utc_s = int(row["utc_s"])
        if utc_s <= 0:
            continue
        value = utc_s * 1_000_000 + int(row.get("nav_usec", 0))
        first = value if first is None else first
        last = value
    return None if first is None or last is None else (first, last)


def fit_spresense_clock(rows: Iterable[dict[str, str]]) -> ClockFit:
    pairs = [
        (
            int(row["received_mono_us"]),
            int(row["utc_s"]) * 1_000_000 + int(row.get("nav_usec", 0)),
        )
        for row in rows
        if int(row["utc_s"]) > 0
    ]
    if len(pairs) < 2:
        raise ValueError("at least two valid GPS UTC rows are required")
    mean_x = sum(x for x, _ in pairs) / len(pairs)
    mean_y = sum(y for _, y in pairs) / len(pairs)
    denominator = sum((x - mean_x) ** 2 for x, _ in pairs)
    if denominator == 0:
        raise ValueError("Spresense monotonic timestamps do not vary")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / denominator
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in pairs]
    rms = math.sqrt(sum(value * value for value in residuals) / len(residuals))
    return ClockFit(intercept, slope, rms, len(pairs))


def _session_bounds(path: Path) -> tuple[int, int]:
    first = None
    last = None
    for row in _read_rows(path):
        value = int(row["utc_us"])
        if value <= 0:
            continue
        first = value if first is None else min(first, value)
        last = value if last is None else max(last, value)
    if first is None or last is None:
        raise ValueError(f"empty C_MAIN IMU file: {path}")
    return first, last


def discover_sessions(c_main_dir: Path, requested: list[str] | None = None) -> list[Session]:
    selected = {value.zfill(5) for value in requested} if requested else None
    sessions = []
    for path in sorted(c_main_dir.glob("C_MAIN_IMU_100Hz_*.csv")):
        match = SESSION_RE.search(path.name)
        if not match:
            continue
        suffix = match.group(1)
        if selected is not None and suffix not in selected:
            continue
        start, end = _session_bounds(path)
        date = datetime.fromtimestamp(start / 1_000_000, UTC).strftime("%Y%m%d")
        sessions.append(Session(suffix, f"{date}_cmain_{suffix[-3:]}", start, end))
    if not sessions:
        raise FileNotFoundError("no matching C_MAIN IMU sessions found")
    return sessions


def find_spresense_pair(spresense_dir: Path, session: Session) -> tuple[Path, Path, ClockFit]:
    candidates = []
    for gps_path in sorted(spresense_dir.glob("G*.CSV")):
        bounds = _first_last_valid_utc(gps_path)
        if bounds is None:
            continue
        overlap_us = min(bounds[1], session.end_utc_us) - max(bounds[0], session.start_utc_us)
        if overlap_us >= 0:
            candidates.append((overlap_us, gps_path))
    if not candidates:
        raise FileNotFoundError(f"no Spresense GPS log overlaps C_MAIN {session.suffix}")
    gps_path = max(candidates, key=lambda item: item[0])[1]
    imu_path = gps_path.with_name("I" + gps_path.name[1:])
    if not imu_path.exists():
        raise FileNotFoundError(f"matching Spresense IMU file is missing: {imu_path}")
    fit = fit_spresense_clock(_read_rows(gps_path))
    return gps_path, imu_path, fit


def _write_normalized_csv(
    source: Path,
    destination: Path,
    session: Session,
    utc_for_row,
) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with source.open("r", newline="", encoding="utf-8-sig") as input_handle:
        reader = csv.DictReader(input_handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {source}")
        fields = ["sync_utc_us", "sync_utc_iso", "experiment_time_s", *reader.fieldnames]
        with destination.open("w", newline="", encoding="utf-8") as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=fields)
            writer.writeheader()
            for row in reader:
                utc_us = utc_for_row(row)
                if utc_us < session.start_utc_us or utc_us > session.end_utc_us:
                    continue
                writer.writerow(
                    {
                        "sync_utc_us": utc_us,
                        "sync_utc_iso": _iso_utc(utc_us),
                        "experiment_time_s": f"{(utc_us - session.start_utc_us) / 1e6:.6f}",
                        **row,
                    }
                )
                count += 1
    return count


def _normalize_c_main(c_main_dir: Path, destination: Path, session: Session) -> list[dict]:
    records = []
    for source in sorted(c_main_dir.glob(f"*_{session.suffix}.csv")):
        with source.open("r", newline="", encoding="utf-8-sig") as handle:
            fields = csv.DictReader(handle).fieldnames or []
        if "utc_us" in fields:
            utc_for_row = lambda row: int(row["utc_us"])
        elif {"mono_us", "utc_anchor_mono_us", "utc_anchor_epoch_us"} <= set(fields):
            utc_for_row = lambda row: (
                int(row["utc_anchor_epoch_us"])
                + int(row["mono_us"])
                - int(row["utc_anchor_mono_us"])
            )
        else:
            continue
        output = destination / "c_main" / source.name
        rows = _write_normalized_csv(source, output, session, utc_for_row)
        records.append({"source": str(source), "output": str(output), "rows": rows})
    return records


def _normalize_spresense(
    gps_path: Path,
    imu_path: Path,
    fit: ClockFit,
    destination: Path,
    session: Session,
) -> list[dict]:
    records = []
    for source in (gps_path, imu_path):
        output = destination / "spresense" / source.name
        rows = _write_normalized_csv(
            source,
            output,
            session,
            lambda row: fit.utc_us(int(row["received_mono_us"])),
        )
        records.append({"source": str(source), "output": str(output), "rows": rows})
    return records


def resolve_ffmpeg(explicit: Path | None = None) -> Path:
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(explicit)
        return explicit.resolve()
    located = shutil.which("ffmpeg")
    if located:
        return Path(located)
    try:
        import imageio_ffmpeg

        return Path(imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError as exc:
        raise RuntimeError(
            "ffmpeg was not found; install the media extra or pass --ffmpeg"
        ) from exc


def probe_media(path: Path, ffmpeg: Path) -> MediaInfo:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(path)],
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    text = result.stderr + result.stdout
    creation_match = CREATION_TIME_RE.search(text)
    duration_match = DURATION_RE.search(text)
    if not creation_match or not duration_match:
        raise ValueError(f"could not read creation time/duration from {path}")
    start = datetime.fromisoformat(creation_match.group(1))
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return MediaInfo(path, round(start.timestamp() * 1_000_000), duration)


def _trim_media(
    ffmpeg: Path,
    info: MediaInfo,
    session: Session,
    output: Path,
) -> dict | None:
    overlap_start = max(info.start_utc_us, session.start_utc_us)
    overlap_end = min(info.end_utc_us, session.end_utc_us)
    if overlap_end <= overlap_start:
        return None
    offset_s = (overlap_start - info.start_utc_us) / 1_000_000
    duration_s = (overlap_end - overlap_start) / 1_000_000
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-ss",
        f"{offset_s:.6f}",
        "-i",
        str(info.path),
        "-t",
        f"{duration_s:.6f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-metadata",
        f"creation_time={_iso_utc(overlap_start)}",
        "-avoid_negative_ts",
        "make_zero",
        "-f",
        "mp4",
        str(output),
    ]
    subprocess.run(command, check=True)
    output_info = probe_media(output, ffmpeg)
    return {
        "source": str(info.path),
        "output": str(output),
        "source_offset_s": offset_s,
        "requested_duration_s": duration_s,
        "output_duration_s": output_info.duration_s,
        "duration_delta_s": output_info.duration_s - duration_s,
        "output_bytes": output.stat().st_size,
        "overlap_start_utc": _iso_utc(overlap_start),
        "overlap_end_utc": _iso_utc(overlap_end),
        "note": "video/audio only; DJI djmd telemetry is stored in camera_telemetry CSV",
    }


def _boxes(handle, start: int, end: int) -> Iterator[tuple[str, int, int, int]]:
    position = start
    while position + 8 <= end:
        handle.seek(position)
        size = struct.unpack(">I", handle.read(4))[0]
        kind = handle.read(4).decode("latin1")
        header = 8
        if size == 1:
            size = struct.unpack(">Q", handle.read(8))[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            break
        yield kind, position, size, header
        position += size


def _children(handle, box) -> Iterator[tuple[str, int, int, int]]:
    return _boxes(handle, box[1] + box[3], box[1] + box[2])


def _one(boxes, kind: str):
    return next(box for box in boxes if box[0] == kind)


def _parse_varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < len(data) and shift < 70:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, position
        shift += 7
    raise ValueError("invalid protobuf varint")


def _proto_fields(data: bytes) -> dict[int, list[tuple[int, object]]]:
    position = 0
    fields: dict[int, list[tuple[int, object]]] = {}
    while position < len(data):
        key, position = _parse_varint(data, position)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value, position = _parse_varint(data, position)
        elif wire == 1:
            value = data[position : position + 8]
            position += 8
        elif wire == 2:
            length, position = _parse_varint(data, position)
            value = data[position : position + length]
            position += length
        elif wire == 5:
            value = data[position : position + 4]
            position += 4
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire}")
        fields.setdefault(number, []).append((wire, value))
    return fields


def _sub(fields, number: int):
    return _proto_fields(fields[number][0][1])


def _var(fields, number: int):
    return fields[number][0][1]


def _var_optional(fields, number: int, default=""):
    return fields[number][0][1] if number in fields else default


def _f32(fields, number: int) -> float:
    return struct.unpack("<f", fields[number][0][1])[0]


def _f64(fields, number: int) -> float:
    return struct.unpack("<d", fields[number][0][1])[0]


def _parse_djmd(data: bytes) -> dict[str, object]:
    top = _proto_fields(data)
    message = _sub(top, 3)
    header = _sub(message, 1)
    camera = _sub(message, 2)
    quaternion = _sub(camera, 9)
    acceleration = _sub(camera, 10)
    result = {
        "camera_frame": _var_optional(header, 1),
        "camera_internal_us": _var(header, 2),
        "qw": _f32(quaternion, 1),
        "qx": _f32(quaternion, 2),
        "qy": _f32(quaternion, 3),
        "qz": _f32(quaternion, 4),
        "accel_x_g": _f32(acceleration, 2),
        "accel_y_g": _f32(acceleration, 3),
        "accel_z_g": _f32(acceleration, 4),
        "iso": _f32(_sub(camera, 3), 1),
        "white_balance_k": _var(_sub(camera, 6), 1),
    }
    try:
        fix = _sub(_sub(message, 4), 2)
        coordinate = _sub(fix, 1)
        result.update(
            {
                "gps_latitude_deg": _f64(coordinate, 2),
                "gps_longitude_deg": _f64(coordinate, 3),
                "gps_altitude_m": _var(fix, 2) / 1000,
            }
        )
    except (KeyError, ValueError, struct.error):
        result.update(
            {"gps_latitude_deg": "", "gps_longitude_deg": "", "gps_altitude_m": ""}
        )
    return result


def _djmd_samples(path: Path) -> Iterator[tuple[float, bytes]]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()
        moov = _one(_boxes(handle, 0, file_size), "moov")
        target = None
        for track in (box for box in _children(handle, moov) if box[0] == "trak"):
            mdia = _one(_children(handle, track), "mdia")
            mdia_children = list(_children(handle, mdia))
            hdlr = _one(mdia_children, "hdlr")
            handle.seek(hdlr[1] + 16)
            if handle.read(4) == b"meta":
                target = mdia
                break
        if target is None:
            raise ValueError(f"DJI metadata track not found: {path}")
        mdia_children = list(_children(handle, target))
        mdhd = _one(mdia_children, "mdhd")
        handle.seek(mdhd[1] + 8)
        version = handle.read(1)[0]
        handle.read(3)
        if version == 0:
            handle.read(8)
            timescale, _duration = struct.unpack(">II", handle.read(8))
        else:
            handle.read(16)
            timescale = struct.unpack(">I", handle.read(4))[0]
            handle.read(8)
        minf = _one(mdia_children, "minf")
        stbl = _one(_children(handle, minf), "stbl")
        table = list(_children(handle, stbl))
        stsd = _one(table, "stsd")
        handle.seek(stsd[1] + 20)
        if handle.read(4) != b"djmd":
            raise ValueError(f"metadata track is not djmd: {path}")
        stsz = _one(table, "stsz")
        handle.seek(stsz[1] + 12)
        constant_size, count = struct.unpack(">II", handle.read(8))
        sizes = [constant_size] * count
        if constant_size == 0:
            sizes = list(struct.unpack(f">{count}I", handle.read(count * 4)))
        offsets_box = next(box for box in table if box[0] in {"stco", "co64"})
        handle.seek(offsets_box[1] + 12)
        chunk_count = struct.unpack(">I", handle.read(4))[0]
        code = "Q" if offsets_box[0] == "co64" else "I"
        unit = 8 if code == "Q" else 4
        chunk_offsets = list(struct.unpack(f">{chunk_count}{code}", handle.read(chunk_count * unit)))
        stsc = _one(table, "stsc")
        handle.seek(stsc[1] + 12)
        entry_count = struct.unpack(">I", handle.read(4))[0]
        entries = [struct.unpack(">III", handle.read(12)) for _ in range(entry_count)]
        samples_per_chunk = []
        for index, (first_chunk, samples, _description) in enumerate(entries):
            end_chunk = entries[index + 1][0] if index + 1 < len(entries) else chunk_count + 1
            samples_per_chunk.extend([samples] * (end_chunk - first_chunk))
        sample_offsets = []
        sample_index = 0
        for chunk_offset, samples in zip(chunk_offsets, samples_per_chunk, strict=True):
            offset = chunk_offset
            for _ in range(samples):
                sample_offsets.append(offset)
                offset += sizes[sample_index]
                sample_index += 1
        stts = _one(table, "stts")
        handle.seek(stts[1] + 12)
        time_entry_count = struct.unpack(">I", handle.read(4))[0]
        time_entries = [struct.unpack(">II", handle.read(8)) for _ in range(time_entry_count)]
        timestamps = []
        media_time = 0
        for sample_count, delta in time_entries:
            for _ in range(sample_count):
                timestamps.append(media_time / timescale)
                media_time += delta
        for timestamp, offset, size in zip(timestamps, sample_offsets, sizes, strict=True):
            handle.seek(offset)
            yield timestamp, handle.read(size)


def extract_camera_telemetry(
    info: MediaInfo,
    session: Session,
    output: Path,
) -> dict | None:
    overlap_start = max(info.start_utc_us, session.start_utc_us)
    overlap_end = min(info.end_utc_us, session.end_utc_us)
    if overlap_end <= overlap_start:
        return None
    fields = [
        "sync_utc_us",
        "sync_utc_iso",
        "experiment_time_s",
        "video_time_s",
        "camera_frame",
        "camera_internal_us",
        "qw",
        "qx",
        "qy",
        "qz",
        "accel_x_g",
        "accel_y_g",
        "accel_z_g",
        "iso",
        "white_balance_k",
        "gps_latitude_deg",
        "gps_longitude_deg",
        "gps_altitude_m",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for video_time_s, data in _djmd_samples(info.path):
            utc_us = info.start_utc_us + round(video_time_s * 1_000_000)
            if utc_us < overlap_start:
                continue
            if utc_us > overlap_end:
                break
            parsed = _parse_djmd(data)
            writer.writerow(
                {
                    "sync_utc_us": utc_us,
                    "sync_utc_iso": _iso_utc(utc_us),
                    "experiment_time_s": f"{(utc_us - session.start_utc_us) / 1e6:.6f}",
                    "video_time_s": f"{video_time_s:.6f}",
                    **parsed,
                }
            )
            count += 1
    return {"source": str(info.path), "output": str(output), "rows": count}


def _camera_outputs(
    raw_root: Path,
    destination: Path,
    session: Session,
    ffmpeg: Path,
    trim_video: bool,
) -> tuple[list[dict], list[dict]]:
    videos = []
    telemetry = []
    for role in ("C_MAIN_CAM", "P_MAIN_CAM"):
        directory = raw_root / role
        media = [probe_media(path, ffmpeg) for path in sorted(directory.glob("*.*"))]
        for info in media:
            if info.path.suffix.upper() == ".LRF":
                telemetry_output = (
                    destination / "camera_telemetry" / f"{role}_{info.path.stem}.csv"
                )
                record = extract_camera_telemetry(info, session, telemetry_output)
                if record:
                    telemetry.append(record)
            if info.path.suffix.upper() not in {".MP4", ".LRF"}:
                continue
            label = "proxy" if info.path.suffix.upper() == ".LRF" else "video"
            output = destination / "camera_video" / f"{role}_{info.path.stem}_{label}.mp4"
            overlap_start = max(info.start_utc_us, session.start_utc_us)
            overlap_end = min(info.end_utc_us, session.end_utc_us)
            if overlap_end <= overlap_start:
                continue
            if trim_video:
                record = _trim_media(ffmpeg, info, session, output)
            else:
                record = {
                    "source": str(info.path),
                    "output": str(output),
                    "source_offset_s": (overlap_start - info.start_utc_us) / 1e6,
                    "requested_duration_s": (overlap_end - overlap_start) / 1e6,
                    "overlap_start_utc": _iso_utc(overlap_start),
                    "overlap_end_utc": _iso_utc(overlap_end),
                    "status": "planned_only",
                }
            if record:
                videos.append(record)
    return videos, telemetry


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable_not_git_repository"


def synchronize_raw_data(
    raw_root: Path,
    interim_root: Path,
    results_root: Path,
    sessions: list[str] | None = None,
    ffmpeg_path: Path | None = None,
    trim_video: bool = True,
) -> list[Path]:
    raw_root = raw_root.resolve()
    interim_root = interim_root.resolve()
    results_root = results_root.resolve()
    discovered = discover_sessions(raw_root / "C_MAIN", sessions)
    ffmpeg = resolve_ffmpeg(ffmpeg_path)
    manifests = []
    for session in discovered:
        destination = interim_root / session.experiment_id
        gps_path, imu_path, fit = find_spresense_pair(raw_root / "Spresense", session)
        sensor_records = _normalize_c_main(raw_root / "C_MAIN", destination, session)
        sensor_records.extend(
            _normalize_spresense(gps_path, imu_path, fit, destination, session)
        )
        video_records, telemetry_records = _camera_outputs(
            raw_root, destination, session, ffmpeg, trim_video
        )
        manifest = {
            "experiment_id": session.experiment_id,
            "c_main_suffix": session.suffix,
            "experiment_epoch_utc_us": session.start_utc_us,
            "start_utc": _iso_utc(session.start_utc_us),
            "end_utc": _iso_utc(session.end_utc_us),
            "duration_s": (session.end_utc_us - session.start_utc_us) / 1e6,
            "git_commit_sha": _git_sha(),
            "spresense_clock_fit": asdict(fit),
            "spresense_pair": [str(gps_path), str(imu_path)],
            "sensor_outputs": sensor_records,
            "camera_outputs": video_records,
            "camera_telemetry_outputs": telemetry_records,
            "warnings": [
                "Camera absolute time uses QuickTime creation_time; IMU cross-correlation fine alignment was not applied.",
                "Trimmed camera files contain video/audio only because ffmpeg cannot remux DJI djmd; telemetry is preserved as CSV from LRF.",
                "Stream-copy trimming may begin at the nearest decodable video keyframe; source offsets remain in this manifest.",
            ],
        }
        report_dir = results_root / session.experiment_id / "synchronization"
        report_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = report_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summary_path = report_dir / "summary.md"
        summary_path.write_text(_summary_markdown(manifest), encoding="utf-8")
        manifests.append(manifest_path)
        print(
            f"{session.experiment_id}: {manifest['start_utc']} to {manifest['end_utc']} "
            f"Spresense={gps_path.name}/{imu_path.name}"
        )
    return manifests


def _summary_markdown(manifest: dict) -> str:
    lines = [
        f"# Synchronization summary: {manifest['experiment_id']}",
        "",
        f"- C_MAIN source suffix: `{manifest['c_main_suffix']}`",
        f"- RTC range (UTC): `{manifest['start_utc']}` – `{manifest['end_utc']}`",
        f"- Duration: `{manifest['duration_s']:.6f} s`",
        (
            f"- Spresense: `{Path(manifest['spresense_pair'][0]).name}` / "
            f"`{Path(manifest['spresense_pair'][1]).name}`"
        ),
        (
            f"- Spresense clock-fit RMS: "
            f"`{manifest['spresense_clock_fit']['residual_rms_us']:.3f} us`"
        ),
        "",
        "## Outputs",
        "",
        f"- Normalized sensor CSV files: {len(manifest['sensor_outputs'])}",
        f"- Trimmed camera files: {len(manifest['camera_outputs'])}",
        f"- Camera telemetry CSV files: {len(manifest['camera_telemetry_outputs'])}",
        "",
        "All normalized CSV files use the C_MAIN start as `experiment_time_s = 0`.",
        "",
        "## Warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in manifest["warnings"])
    lines.append("")
    return "\n".join(lines)
