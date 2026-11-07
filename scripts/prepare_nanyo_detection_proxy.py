"""Create explicit, timestamp-preserving 30 Hz detection proxies from processed MP4."""
import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import imageio_ffmpeg

from aquabeacon_analysis.ngt_binary import sha256

ROOT = Path(__file__).resolve().parents[1]


def prepare(eid, *, duration=None):
    registry = ROOT / f"experiments/{eid}/experiment.json"
    if not registry.is_file() or "_nanyo_" not in eid:
        raise ValueError("Explicit registered Nanyo trial required")
    source = ROOT / f"data/processed/{eid}/videos/parent.mp4"
    out = ROOT / f"data/interim/{eid}/optical_detection"
    out.mkdir(parents=True, exist_ok=True)
    name = "parent_30hz" if duration is None else f"pilot_{duration:g}s"
    target = out / (name + ".mp4")
    manifest = target.with_suffix(".json")
    if target.exists() or manifest.exists():
        raise FileExistsError(f"Preserving existing proxy: {target}")
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "warning",
               "-nostdin", "-n", "-threads", "2", "-hwaccel", "cuda",
               "-hwaccel_output_format", "cuda", "-noautorotate", "-i", str(source)]
    if duration is not None:
        command += ["-t", str(duration)]
    command += ["-an", "-vf", ("select=not(mod(n\\,4)),hwdownload,format=p010le,"
                "scale=1280:720:flags=area,format=yuv420p"), "-filter_threads", "1",
                "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "lossless",
                "-fps_mode", "passthrough", str(target)]
    stat = source.stat()
    meta = {"experiment_id": eid, "source": str(source.relative_to(ROOT)),
            "source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
            "source_read_only": True, "command": command, "duration_limit_s": duration,
            "clock": "Every fourth decoded source frame; original presentation timestamps retained",
            "pixels": "1920x1080 encoded full field resampled to 1280x720, 10 to 8 bit; lossless H264 encode",
            "hardware": "NVIDIA decode/encode; 2 CPU decoder threads, 1 filter thread",
            "created_utc": datetime.now(UTC).isoformat(), "task_owned": True,
            "code_sha256": sha256(Path(__file__))}
    with target.with_suffix(".log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
        meta.update(pid=process.pid, started_utc=datetime.now(UTC).isoformat(), status="running")
        manifest.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(eid, "proxy PID", process.pid, meta["started_utc"], flush=True)
        try:
            deadline = time.monotonic()+900
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("Detection proxy timeout")
                time.sleep(0.5)
            if process.returncode:
                raise RuntimeError(f"Proxy encoder exit {process.returncode}: {target.with_suffix('.log')}")
            if (source.stat().st_size, source.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise RuntimeError("Processed input changed during proxy creation")
            meta.update(status="completed", sha256=sha256(target))
        except BaseException as exc:
            meta.update(status="failed", error=str(exc))
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            meta.update(ended_utc=datetime.now(UTC).isoformat(), returncode=process.returncode)
            manifest.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(eid, "proxy complete", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--duration", type=float)
    args = parser.parse_args()
    print("proxy manager", os.getpid(), datetime.now(UTC).isoformat(), flush=True)
    for experiment in dict.fromkeys(args.experiment):
        prepare(experiment, duration=args.duration)
