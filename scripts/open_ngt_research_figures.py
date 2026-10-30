"""List explicitly selected experiments' local 2D masters and launch the shared editor."""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquabeacon_analysis.editable_plots import PlotDocument, gallery_html, make_server


def collect_figures(experiment: str) -> list[dict]:
    """Accept experiment IDs, enumerate local 2D masters, and retain no loaded figures."""
    if not experiment or Path(experiment).name != experiment or any(c in experiment for c in "/\\:"):
        raise ValueError("実験IDを指定してください（パス指定は使用できません）")
    if not (ROOT / "experiments" / experiment / "experiment.json").is_file():
        raise ValueError(f"未登録の実験IDです: {experiment}")
    folder = ROOT / "results" / experiment
    entries = []
    for master in sorted(folder.rglob("*.figure.pickle")):
        if not master.resolve().is_relative_to(folder.resolve()):
            continue
        if any(part in {"editor_validation", "editor_roundtrip", "research_figures"}
               for part in master.relative_to(folder).parts[:-1]):
            continue
        preview = master.with_name(master.name.removesuffix(".figure.pickle") + ".png")
        if not preview.is_file():
            continue
        document = PlotDocument(master)
        if not document.figure.axes or any(axis.name == "3d" for axis in document.figure.axes):
            del document
            gc.collect()
            continue
        titles = [axis.get_title() or axis.get_ylabel() for axis in document.figure.axes]
        name = master.name.removesuffix(".figure.pickle")
        source = master.relative_to(folder).as_posix()
        entries.append({
            "experiment": experiment, "title": f"{experiment} / {name}",
            "description": " / ".join(filter(None, titles)) or source,
            "source_relative": source, "master": str(master.resolve()),
            "preview": str(preview.resolve()),
            "preview_relative": Path(os.path.relpath(preview, folder / "research_figures")).as_posix(),
            "command": f'python -m aquabeacon_analysis.editable_plots "{master.relative_to(ROOT).as_posix()}"',
            "panels": len(document.figure.axes),
        })
        del document
        gc.collect()
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", action="append", required=True)
    parser.add_argument("--figure", help="exact source_relative value from gallery.json")
    parser.add_argument("--catalog-only", action="store_true", help="write list without starting a server")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--idle-timeout", type=float, default=1800)
    args = parser.parse_args(argv)
    entries = []
    for experiment in dict.fromkeys(args.experiment):
        selected = collect_figures(experiment)
        folder = ROOT / "results" / experiment / "research_figures"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "gallery.json").write_text(json.dumps({
            "generated_utc": datetime.now(UTC).isoformat(), "experiment": experiment,
            "scope": "existing local 2D masters only; no figure regeneration",
            "figures": selected,
            "browser_checks": "Not automated. Verify selection, controls, save/reload, PNG/SVG/PDF and save-and-exit manually.",
            "launcher": f"python scripts/open_ngt_research_figures.py --experiment {experiment}",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (folder / "index.html").write_text(gallery_html(selected), encoding="utf-8")
        print(f"{experiment}: {len(selected)} 2D figures; {folder / 'index.html'}", flush=True)
        entries.extend(selected)
    if args.catalog_only:
        return 0
    if args.figure:
        entries = [entry for entry in entries if entry["source_relative"] == args.figure]
    if not entries:
        parser.error("対象の2D図がありません。gallery.jsonのsource_relativeを確認してください")
    server = make_server(PlotDocument(Path(entries[0]["master"])), args.port,
                         args.idle_timeout, gallery=entries)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"PID {os.getpid()} started {datetime.now(UTC).isoformat()}: research figure editor\n"
          f"{url}\nStop: save-and-exit, Ctrl+C, or idle timeout {args.idle_timeout:g}s", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
