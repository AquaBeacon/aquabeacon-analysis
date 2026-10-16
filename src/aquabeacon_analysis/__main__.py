from __future__ import annotations

import argparse
from pathlib import Path

from .imu_localization import localize_all
from .sync_analysis import analyze_synchronization
from .time_sync import synchronize_raw_data
from .tof_preprocess import preprocess_all


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aquabeacon_analysis")
    subparsers = parser.add_subparsers(dest="command")
    sync = subparsers.add_parser(
        "sync-raw",
        help="trim raw sensors and cameras to C_MAIN RTC time ranges",
    )
    sync.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    sync.add_argument("--interim-root", type=Path, default=Path("data/interim"))
    sync.add_argument("--results-root", type=Path, default=Path("results"))
    sync.add_argument(
        "--session",
        action="append",
        dest="sessions",
        help="C_MAIN numeric suffix to process; repeat for multiple sessions",
    )
    sync.add_argument(
        "--ffmpeg",
        type=Path,
        help="path to ffmpeg; otherwise PATH or imageio-ffmpeg is used",
    )
    sync.add_argument(
        "--skip-video",
        action="store_true",
        help="write sensor/telemetry outputs and video trim plan without video files",
    )
    analyze = subparsers.add_parser(
        "analyze-sync",
        help="refine IMU synchronization, plot diagnostics, and check data quality",
    )
    analyze.add_argument("--interim-root", type=Path, default=Path("data/interim"))
    analyze.add_argument("--results-root", type=Path, default=Path("results"))
    analyze.add_argument(
        "--session",
        action="append",
        dest="sessions",
        help="experiment id or C_MAIN suffix; repeat for multiple sessions",
    )
    preprocess = subparsers.add_parser(
        "preprocess-tof", help="extract sustained ToF windows and horizontal C_MAIN video"
    )
    preprocess.add_argument("--interim-root", type=Path, default=Path("data/interim"))
    preprocess.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    preprocess.add_argument("--results-root", type=Path, default=Path("results"))
    preprocess.add_argument("--session", action="append", dest="sessions")
    localize = subparsers.add_parser(
        "localize-imu", help="estimate sensor attitudes and parent relative IMU trajectory"
    )
    localize.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    localize.add_argument("--results-root", type=Path, default=Path("results"))
    localize.add_argument("--session", action="append", dest="sessions")
    lights = subparsers.add_parser("detect-lights", help="locate child light in parent camera footage")
    lights.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    lights.add_argument("--results-root", type=Path, default=Path("results"))
    lights.add_argument("--session", action="append", dest="sessions")
    for field in ("fx", "fy", "cx", "cy"):
        lights.add_argument(f"--{field}", type=float)
    track = subparsers.add_parser(
        "track-lights", help="track visually seeded child lamp with an extended Kalman filter"
    )
    track.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    track.add_argument("--results-root", type=Path, default=Path("results"))
    track.add_argument("--session", action="append", dest="sessions")
    for field in ("fx", "fy", "cx", "cy"):
        track.add_argument(f"--{field}", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "sync-raw":
        synchronize_raw_data(
            raw_root=args.raw_root,
            interim_root=args.interim_root,
            results_root=args.results_root,
            sessions=args.sessions,
            ffmpeg_path=args.ffmpeg,
            trim_video=not args.skip_video,
        )
        return 0
    if args.command == "analyze-sync":
        analyze_synchronization(
            interim_root=args.interim_root,
            results_root=args.results_root,
            sessions=args.sessions,
        )
        return 0
    if args.command == "preprocess-tof":
        for manifest in preprocess_all(
            args.interim_root, args.processed_root, args.results_root,
            Path.cwd().resolve(), args.sessions,
        ):
            print(manifest)
        return 0
    if args.command == "localize-imu":
        for path in localize_all(args.processed_root, args.results_root, Path.cwd().resolve(),
                                 args.sessions):
            print(path)
        return 0
    if args.command == "detect-lights":
        from .light_detection import Intrinsics
        from .light_detection import analyze_all as analyze_lights

        values = (args.fx, args.fy, args.cx, args.cy)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            parser.error("--fx, --fy, --cx, and --cy must be supplied together")
        intrinsics = Intrinsics(*values) if all(value is not None for value in values) else None
        for manifest in analyze_lights(
            args.processed_root, args.results_root, intrinsics, args.sessions
        ):
            print(manifest)
        return 0
    if args.command == "track-lights":
        from .light_detection import Intrinsics
        from .light_tracking import track_all

        values = (args.fx, args.fy, args.cx, args.cy)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            parser.error("--fx, --fy, --cx, and --cy must be supplied together")
        intrinsics = Intrinsics(*values) if all(value is not None for value in values) else None
        for manifest in track_all(
            args.processed_root, args.results_root, args.sessions, intrinsics
        ):
            print(manifest)
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
