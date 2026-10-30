from __future__ import annotations

import argparse
import json
from pathlib import Path

from .experiments import (
    describe_experiment,
    experiment_context,
    initialize_experiment,
    load_experiment,
    repository_path,
)
from .imu_localization import localize_all
from .sync_analysis import analyze_synchronization
from .time_sync import synchronize_raw_data
from .tof_preprocess import preprocess_all


def _add_scope(parser: argparse.ArgumentParser) -> None:
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--experiment", help="registered experiment ID (recommended)")
    scope.add_argument("--session", action="append", dest="sessions",
                       help="legacy experiment ID or unambiguous numeric suffix; repeatable")
    scope.add_argument("--all-experiments", action="store_true",
                       help="explicitly process all compatible experiments under the input root")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aquabeacon_analysis")
    subparsers = parser.add_subparsers(dest="command")
    initialize = subparsers.add_parser("init-experiment", help="create an isolated experiment registry")
    initialize.add_argument("experiment_id")
    initialize.add_argument("--c-main-suffix", help="actual C_MAIN log number, independent of run ID")
    initialize.add_argument("--p-main-suffix", help="actual P_MAIN log number, if known")
    subparsers.add_parser("list-experiments", help="list registered experiments and local data status")
    show = subparsers.add_parser("show-experiment", help="show paths and missing registered references")
    show.add_argument("experiment_id")
    sync = subparsers.add_parser(
        "sync-raw",
        help="trim raw sensors and cameras to C_MAIN RTC time ranges",
    )
    sync.add_argument("--raw-root", type=Path)
    sync.add_argument("--interim-root", type=Path)
    sync.add_argument("--results-root", type=Path)
    _add_scope(sync)
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
    analyze.add_argument("--interim-root", type=Path)
    analyze.add_argument("--results-root", type=Path)
    _add_scope(analyze)
    preprocess = subparsers.add_parser(
        "preprocess-tof", help="extract sustained ToF windows and horizontal C_MAIN video"
    )
    preprocess.add_argument("--interim-root", type=Path)
    preprocess.add_argument("--processed-root", type=Path)
    preprocess.add_argument("--results-root", type=Path)
    _add_scope(preprocess)
    localize = subparsers.add_parser(
        "localize-imu", help="estimate sensor attitudes and parent relative IMU trajectory"
    )
    localize.add_argument("--processed-root", type=Path)
    localize.add_argument("--results-root", type=Path)
    _add_scope(localize)
    lights = subparsers.add_parser("detect-lights", help="locate child light in parent camera footage")
    lights.add_argument("--processed-root", type=Path)
    lights.add_argument("--results-root", type=Path)
    _add_scope(lights)
    for field in ("fx", "fy", "cx", "cy"):
        lights.add_argument(f"--{field}", type=float)
    track = subparsers.add_parser(
        "track-lights", help="track visually seeded child lamp with an extended Kalman filter"
    )
    track.add_argument("--processed-root", type=Path)
    track.add_argument("--results-root", type=Path)
    _add_scope(track)
    for field in ("fx", "fy", "cx", "cy"):
        track.add_argument(f"--{field}", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    repository = Path.cwd().resolve()
    try:
        if args.command == "init-experiment":
            experiment = initialize_experiment(repository, args.experiment_id, args.c_main_suffix,
                                               args.p_main_suffix)
            print(experiment.metadata_path)
            print(f"Import originals separately into {experiment.raw_root}; raw was not created.")
            return 0
        if args.command == "list-experiments":
            for path in sorted((repository / "experiments").glob("*/experiment.json")):
                details = describe_experiment(load_experiment(repository, path.parent.name))
                status = ", ".join(f"{key}={'present' if item['exists'] else 'missing'}"
                                   for key, item in details["paths"].items())
                sources = details["reference_status"]["raw_sources"]
                present = sum(source["exists"] for source in sources)
                print(f"{details['experiment_id']}: {status}, raw_sources={present}/{len(sources)}")
            return 0
        if args.command == "show-experiment":
            print(json.dumps(describe_experiment(load_experiment(repository, args.experiment_id)),
                             ensure_ascii=False, indent=2))
            return 0
        experiment = load_experiment(repository, args.experiment) if args.experiment else None
        roots = {"raw_root": "data/raw", "interim_root": "data/interim",
                 "processed_root": "data/processed", "results_root": "results"}
        for name, default in roots.items():
            if not hasattr(args, name):
                continue
            if experiment and getattr(args, name) is not None:
                raise ValueError(f"--experiment cannot be combined with --{name.replace('_', '-')}")
            path = getattr(args, name)
            setattr(args, name, path.resolve() if path else repository / default)
        if experiment:
            args.sessions = [experiment.experiment_id]
            if args.command == "sync-raw":
                suffix = experiment.metadata.get("c_main_suffix")
                if suffix is None:
                    raise ValueError("register c_main_suffix in experiment.json before sync-raw")
                args.raw_root = experiment.raw_root
                args.sessions = [suffix]
        raw_parent = repository_path(repository, "data/raw")
        input_name = {"sync-raw": "raw_root", "analyze-sync": "interim_root",
                      "preprocess-tof": "interim_root"}.get(args.command, "processed_root")
        input_root = getattr(args, input_name).resolve()
        output_names = {"sync-raw": ("interim_root", "results_root"),
                        "preprocess-tof": ("processed_root", "results_root")}.get(
                            args.command, ("results_root",))
        for name in output_names:
            output = getattr(args, name).resolve()
            if output.is_relative_to(raw_parent) or raw_parent.is_relative_to(output):
                raise ValueError(f"output root overlaps protected data/raw: {output}")
            if output.is_relative_to(input_root) or input_root.is_relative_to(output):
                raise ValueError(f"input and output roots overlap: {output}")
        context = experiment_context(experiment, args.command) if experiment else None
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))

    try:
        outputs = _run_analysis(args, experiment.experiment_id if experiment else None)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if not outputs:
        parser.error("no compatible inputs were processed; check the selected experiment and stage")
    if context is not None:
        analysis = {"sync-raw": "synchronization", "analyze-sync": "synchronization/refined",
                    "preprocess-tof": "preprocessing", "localize-imu": "localization",
                    "detect-lights": "light_detection", "track-lights": "light_detection"}[args.command]
        filename = f"experiment_context_{args.command.replace('-', '_')}.json"
        destination = experiment.stage_path("results") / analysis / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    for output in outputs:
        print(output)
    return 0


def _run_analysis(args: argparse.Namespace, experiment_id: str | None) -> list[Path]:
    if args.command == "sync-raw":
        return synchronize_raw_data(
            raw_root=args.raw_root,
            interim_root=args.interim_root,
            results_root=args.results_root,
            sessions=args.sessions,
            ffmpeg_path=args.ffmpeg,
            trim_video=not args.skip_video,
            experiment_id=experiment_id,
        )
    if args.command == "analyze-sync":
        return analyze_synchronization(
            interim_root=args.interim_root,
            results_root=args.results_root,
            sessions=args.sessions,
        )
    if args.command == "preprocess-tof":
        return preprocess_all(
            args.interim_root, args.processed_root, args.results_root,
            Path.cwd().resolve(), args.sessions,
        )
    if args.command == "localize-imu":
        return localize_all(args.processed_root, args.results_root, Path.cwd().resolve(),
                            args.sessions)
    if args.command == "detect-lights":
        from .light_detection import Intrinsics
        from .light_detection import analyze_all as analyze_lights

        values = (args.fx, args.fy, args.cx, args.cy)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("--fx, --fy, --cx, and --cy must be supplied together")
        intrinsics = Intrinsics(*values) if all(value is not None for value in values) else None
        return analyze_lights(
            args.processed_root, args.results_root, intrinsics, args.sessions
        )
    if args.command == "track-lights":
        from .light_detection import Intrinsics
        from .light_tracking import track_all

        values = (args.fx, args.fy, args.cx, args.cy)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("--fx, --fy, --cx, and --cy must be supplied together")
        intrinsics = Intrinsics(*values) if all(value is not None for value in values) else None
        return track_all(
            args.processed_root, args.results_root, args.sessions, intrinsics
        )
    raise ValueError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
