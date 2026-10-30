"""Read-only adapters for the experiment firmware's authoritative binary readers.

No clocks, units, calibrations, or missing values are changed here. The firmware
checkout is an explicit dependency and its reader hashes are recorded on export.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import struct
import subprocess
import zlib
from contextlib import ExitStack
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_reader(firmware_root: Path, name: str):
    """Load one reviewed firmware module without modifying sys.path."""
    path = firmware_root / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_ngt_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decode_scal(raw: bytes) -> dict:
    """Decode SCAL v1 per common/spresense_calibration.h; reject corrupt records."""
    if (len(raw) != 64 or raw[:5] != b"SCAL\x01" or raw[5] > 1
            or any(raw[6:8]) or any(raw[56:60])
            or zlib.crc32(raw[:60]) != struct.unpack_from("<I", raw, 60)[0]):
        raise ValueError("Invalid SCAL header/reserved/CRC")
    generation, *values = struct.unpack_from("<I11f", raw, 8)
    gravity, latitude, *coefficients = values
    import math

    if (not generation or not all(math.isfinite(v) for v in values)
            or not 9.7 <= gravity <= 9.9 or not -90 <= latitude <= 90
            or any(abs(v) > .5 for v in coefficients[:3])
            or any(not .95 <= v <= 1.05 for v in coefficients[3:6])
            or any(abs(v) > .02 for v in coefficients[6:])):
        raise ValueError("Invalid SCAL coefficients")
    return {"generation": generation, "enabled": bool(raw[5]), "gravity_m_s2": gravity,
            "latitude_deg": latitude, "accel_bias_m_s2": coefficients[:3],
            "accel_scale": coefficients[3:6], "gyro_bias_rad_s": coefficients[6:],
            "creation_time": None, "creation_time_note": "SCAL has no timestamp field"}


def export_binary(source: Path, destination: Path, firmware_root: Path,
                  allow_truncated_tail: bool = False) -> dict:
    """Stream CRC-checked records to exclusive CSVs, with provenance and clock extents."""
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or source in destination.parents:
        raise ValueError("Destination must not be inside source")
    destination.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as handle:
        prefix = handle.read(8)
    reader_name = "spresense_log" if prefix == b"AQSBIN01" else "binary_log"
    reader = load_reader(firmware_root, reader_name)
    report = {"source": str(source), "source_sha256": sha256(source),
              "reader": str(firmware_root / "scripts" / f"{reader_name}.py"),
              "reader_sha256": sha256(firmware_root / "scripts" / f"{reader_name}.py"),
              "allow_truncated_tail": allow_truncated_tail, "streams": {}}
    report["git_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], text=True).strip()
    framing = {}
    with ExitStack() as stack:
        writers = {}

        def save(name, row):
            if name not in writers:
                target = destination / f"{source.stem}.{name}.csv"
                output = stack.enter_context(target.open("x", encoding="utf-8", newline=""))
                writer = csv.DictWriter(output, fieldnames=list(row))
                writer.writeheader()
                writers[name] = writer
                report["streams"][name] = {"csv": str(target), "rows": 0,
                                           "columns": list(row), "clock_extents": {}}
            writers[name].writerow(row)
            info = report["streams"][name]
            info["rows"] += 1
            for key, value in row.items():
                if (key.endswith("_us") or key in ("utc_s", "utc_reference_ms")) and isinstance(
                        value, (int, float)):
                    extent = info["clock_extents"].setdefault(key, [value, value])
                    extent[0] = min(extent[0], value)
                    extent[1] = max(extent[1], value)

        if prefix == b"AQSBIN01":
            for row in reader.iter_rows(source, allow_truncated_tail, framing):
                save("samples" if "sample_mono_us" in row else "references", row)
        else:
            handle = stack.enter_context(source.open("rb"))
            report["header"] = reader.read_header(handle)
            for record in reader.records(handle, allow_truncated_tail, framing):
                name = reader.NAMES.get(record["kind"], f"unknown_{record['kind']}")
                for row in reader.rows(record):
                    save(name, row)
    report["framing"] = framing
    report["complete"] = not (framing.get("truncated") or framing.get("truncated_tail_bytes"))
    for info in report["streams"].values():
        info["csv_sha256"] = sha256(Path(info["csv"]))
    target = destination / f"{source.name}.decode.json"
    with target.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    return report


def export_spresense_bin_means(sources: list[Path], target: Path,
                               firmware_root: Path, bin_us: int = 10_000) -> dict:
    """UTC-bin means for synchronization; preserve sample counts and rejected counts.

    Missing bins are absent rather than filled. Only quality 2 with nonzero UTC
    participates. Clock gaps and source boundaries never join disjoint bins.
    """
    reader = load_reader(firmware_root, "spresense_log")
    fields = ["gx", "gy", "gz", "ax", "ay", "az", "corrected_gx", "corrected_gy",
              "corrected_gz", "corrected_ax", "corrected_ay", "corrected_az"]
    info = {"bin_us": bin_us, "rows": 0, "rejected_no_pps_utc": 0, "sources": [],
            "method": "Arithmetic means in floor(sample_utc_us/bin_us) bins; no interpolation"}
    with target.open("x", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["sample_utc_us", "sample_mono_us", "count", *fields])
        for source in sources:
            info["sources"].append({"path": str(source), "sha256": sha256(source)})
            bucket = None
            count = 0
            sums = []

            def flush(count, sums):
                if count:
                    writer.writerow([round(sums[0] / count), round(sums[1] / count), count,
                                     *(value / count for value in sums[2:])])
                    info["rows"] += 1

            for row in reader.iter_rows(source):
                utc = row["sample_utc_us"]
                if not utc or row["time_quality"] != 2:
                    info["rejected_no_pps_utc"] += 1
                    continue
                key = utc // bin_us
                values = [utc, row["sample_mono_us"], *(row[k] for k in fields)]
                if key != bucket:
                    flush(count, sums)
                    bucket, count, sums = key, 0, [0] * len(values)
                count += 1
                sums = [a + b for a, b in zip(sums, values)]
            flush(count, sums)
    info["csv_sha256"] = sha256(target)
    target.with_suffix(".json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info
