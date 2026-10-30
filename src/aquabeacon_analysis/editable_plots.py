"""Preserve Matplotlib figures and edit their presentation in a local browser."""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import io
import json
import os
import pickle
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import matplotlib
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import Collection, PathCollection
from matplotlib.colors import to_hex
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties, findfont, fontManager
from matplotlib.lines import Line2D
from matplotlib.markers import MarkerStyle
from matplotlib.patches import Patch
from matplotlib.ticker import AutoLocator, AutoMinorLocator, MultipleLocator


def available_fonts() -> list[str]:
    """List font families already discovered by Matplotlib on this PC."""
    return sorted({font.name for font in fontManager.ttflist}, key=str.casefold)


def _validate_font_size(value) -> float:
    size = float(value)
    if not np.isfinite(size) or not 1 <= size <= 144:
        raise ValueError("文字サイズは1〜144 ptで指定してください")
    return size


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_editable_figure(figure: Figure, destination: Path | str, **kwargs) -> None:
    """Save the requested image, a vector copy, and a trusted-local editable master.

    The pickle holds the original plotted arrays, artists, axes and annotations.
    Keep the generating script and analysis manifest for long-term reproducibility;
    Matplotlib pickles are version-dependent and must never be loaded from strangers.
    Existing user style overrides are preserved on regeneration.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(destination, **kwargs)
    with matplotlib.rc_context({"svg.fonttype": "none"}):
        figure.savefig(destination.with_suffix(".svg"),
                       **{key: value for key, value in kwargs.items() if key != "format"})
    master = destination.with_suffix(".figure.pickle")
    master.write_bytes(pickle.dumps(figure, protocol=pickle.HIGHEST_PROTOCOL))
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = None
    _write_json(destination.with_suffix(".plot.json"), {
        "schema_version": 1, "created_utc": datetime.now(UTC).isoformat(),
        "python": sys.version, "matplotlib": matplotlib.__version__,
        "git_commit": revision, "generator": sys.argv,
        "master": master.name, "image": destination.name,
        "master_sha256": hashlib.sha256(master.read_bytes()).hexdigest(),
        "savefig": {k: v for k, v in kwargs.items() if k != "format"},
        "reproduction": "Keep the generating script, config, calibration and analysis manifest.",
    })


def _uniform(values) -> float | None:
    values = np.asarray(values)
    if values.size and np.all(values == values.flat[0]):
        return float(values.flat[0])
    return None


def _color(values) -> str | None:
    values = np.asarray(values)
    if values.size and np.all(values == values[0]):
        return to_hex(values[0], keep_alpha=True)
    return None


def artists(axis) -> list:
    return [*axis.lines, *axis.collections, *axis.patches, *axis.texts]


def _describe_2d(axis) -> dict:
    """Expose scalar presentation properties without coercing heterogeneous artists."""
    bounds = axis.get_position().bounds
    values = dict(zip(("left", "bottom", "panel_width", "panel_height"), bounds))
    values.update(xscale=axis.get_xscale(), yscale=axis.get_yscale(),
                  aspect=str(axis.get_aspect()),
                  title_x=axis.title.get_position()[0], title_y=axis.title.get_position()[1],
                  xlabel_x=axis.xaxis.label.get_position()[0],
                  ylabel_y=axis.yaxis.label.get_position()[1])
    for dim in ("x", "y"):
        coordinate = getattr(axis, f"{dim}axis")
        ticks = coordinate.get_major_ticks()
        values[f"{dim}ticks_visible"] = any(t.tick1line.get_visible() for t in ticks)
        values[f"{dim}tick_color"] = to_hex(ticks[0].tick1line.get_color()) if ticks else "#000000"
        values[f"{dim}minor_step"] = None
        values[f"{dim}tick_positions"] = ""
        values[f"{dim}tick_labels"] = ""
    for name, spine in axis.spines.items():
        values[f"spine_{name}_visible"] = spine.get_visible()
        values[f"spine_{name}_color"] = to_hex(spine.get_edgecolor(), keep_alpha=True)
    for which in ("major", "minor"):
        lines = [tick.gridline for coordinate in (axis.xaxis, axis.yaxis)
                 for tick in getattr(coordinate, f"get_{which}_ticks")()]
        values[f"grid_{which}"] = any(line.get_visible() for line in lines)
        if lines:
            values[f"grid_{which}_color"] = to_hex(lines[0].get_color(), keep_alpha=True)
            values[f"grid_{which}_linewidth"] = lines[0].get_linewidth()
            values[f"grid_{which}_linestyle"] = lines[0].get_linestyle()
            values[f"grid_{which}_alpha"] = lines[0].get_alpha()
    return values


def describe_figure(figure: Figure) -> dict:
    result = {"figure": {
        "width": float(figure.get_figwidth()), "height": float(figure.get_figheight()),
        "dpi": float(figure.dpi), "title": figure._suptitle.get_text() if figure._suptitle else "",
        "font": figure.axes[0].xaxis.label.get_fontfamily()[0] if figure.axes else "DejaVu Sans",
        "font_size": None,
        "title_size": figure._suptitle.get_fontsize() if figure._suptitle else 14,
        "background": to_hex(figure.get_facecolor(), keep_alpha=True),
    }, "axes": []}
    for axis in figure.axes:
        legend = axis.get_legend()
        entry = {
            "title": axis.get_title(), "title_size": axis.title.get_fontsize(),
            "xlabel": axis.get_xlabel(), "ylabel": axis.get_ylabel(),
            "label_size": axis.xaxis.label.get_fontsize(),
            "tick_size": axis.xaxis.get_ticklabels()[0].get_fontsize()
            if axis.xaxis.get_ticklabels() else 10,
            "xmin": float(axis.get_xlim()[0]), "xmax": float(axis.get_xlim()[1]),
            "ymin": float(axis.get_ylim()[0]), "ymax": float(axis.get_ylim()[1]),
            "legend": legend is not None and legend.get_visible(),
            "grid": any(line.get_visible() for line in
                        [*axis.get_xgridlines(), *axis.get_ygridlines()]),
            "xtick_step": float(np.diff(axis.get_xticks())[0])
            if isinstance(axis.xaxis.get_major_locator(), MultipleLocator) else None,
            "ytick_step": float(np.diff(axis.get_yticks())[0])
            if isinstance(axis.yaxis.get_major_locator(), MultipleLocator) else None,
            "xtick_rotation": axis.get_xticklabels()[0].get_rotation()
            if axis.get_xticklabels() else 0,
            "ytick_rotation": axis.get_yticklabels()[0].get_rotation()
            if axis.get_yticklabels() else 0,
            "legend_location": str(legend._loc) if legend else "best",
            "legend_size": legend.get_texts()[0].get_fontsize()
            if legend and legend.get_texts() else 10,
            "artists": [],
        }
        if axis.name != "3d":
            entry.update(_describe_2d(axis))
        entry.update(legend_columns=legend._ncols if legend else 1,
                     legend_frame=legend.get_frame_on() if legend else True)
        if hasattr(axis, "get_zlabel"):
            entry.update(zlabel=axis.get_zlabel(), zmin=float(axis.get_zlim()[0]),
                         zmax=float(axis.get_zlim()[1]), elev=axis.elev, azim=axis.azim)
        for artist in artists(axis):
            item = {"kind": type(artist).__name__, "label": artist.get_label(),
                    "visible": artist.get_visible(), "alpha": artist.get_alpha()}
            if isinstance(artist, Line2D):
                item.update(color=to_hex(artist.get_color(), keep_alpha=True),
                            linewidth=artist.get_linewidth(), linestyle=artist.get_linestyle(),
                            marker=str(artist.get_marker()), markersize=artist.get_markersize(),
                            markeredgewidth=artist.get_markeredgewidth(),
                            markerfacecolor=to_hex(artist.get_markerfacecolor(), keep_alpha=True),
                            markeredgecolor=to_hex(artist.get_markeredgecolor(), keep_alpha=True))
            elif isinstance(artist, Collection):
                item.update(facecolor=_color(artist.get_facecolors()),
                            edgecolor=_color(artist.get_edgecolors()),
                            linewidth=_uniform(artist.get_linewidths()))
                if isinstance(artist, PathCollection):
                    item["size"] = _uniform(artist.get_sizes())
            elif isinstance(artist, Patch):
                item.update(facecolor=to_hex(artist.get_facecolor(), keep_alpha=True),
                            edgecolor=to_hex(artist.get_edgecolor(), keep_alpha=True),
                            linewidth=artist.get_linewidth())
            elif hasattr(artist, "get_text"):
                item.update(text=artist.get_text(), fontsize=artist.get_fontsize(),
                            color=to_hex(artist.get_color(), keep_alpha=True))
                if axis.name != "3d":
                    item.update(position_x=artist.get_position()[0],
                                position_y=artist.get_position()[1])
            entry["artists"].append(item)
        result["axes"].append(entry)
    return result


def _apply_2d(axis, options: dict) -> None:
    for dim in ("x", "y"):
        scale = options.get(f"{dim}scale")
        if scale is not None:
            if scale not in ("linear", "log", "symlog"):
                raise ValueError("軸スケールはlinear/log/symlogから選んでください")
            limits = [options.get(f"{dim}min", getattr(axis, f"get_{dim}lim")()[0]),
                      options.get(f"{dim}max", getattr(axis, f"get_{dim}lim")()[1])]
            if scale == "log" and min(limits) <= 0:
                raise ValueError("対数軸には正の表示範囲を明示してください")
            getattr(axis, f"set_{dim}scale")(scale)
        coordinate = getattr(axis, f"{dim}axis")
        if f"{dim}minor_step" in options:
            step = float(options[f"{dim}minor_step"])
            span = abs(np.diff(getattr(axis, f"get_{dim}lim")())[0])
            if not np.isfinite(step) or step < 0 or (step and span / step > 500):
                raise ValueError("補助目盛りは0（自動）または500本以下の正の間隔にしてください")
            if coordinate.get_scale() != "linear":
                raise ValueError("補助目盛りの間隔指定は線形軸で使用してください")
            coordinate.set_minor_locator(MultipleLocator(step) if step else AutoMinorLocator())
        position_key = f"{dim}tick_positions"
        if position_key in options:
            text = str(options[position_key]).strip()
            if text:
                positions = [float(value.strip()) for value in text.split(",")]
                if len(positions) > 500 or not np.isfinite(positions).all():
                    raise ValueError("目盛り位置は500個以下の有限数をカンマ区切りで指定してください")
                labels = options.get(f"{dim}tick_labels", "")
                labels = str(labels).split("|") if labels else None
                if labels is not None and len(labels) != len(positions):
                    raise ValueError("目盛り位置と目盛り文字の個数を合わせてください")
                getattr(axis, f"set_{dim}ticks")(positions, labels=labels)
            else:
                coordinate.set_major_locator(AutoLocator())
        elif options.get(f"{dim}tick_labels"):
            raise ValueError("目盛り文字は目盛り位置と合わせて指定してください")
        if f"{dim}ticks_visible" in options:
            axis.tick_params(axis=dim, which="both",
                             **{("bottom" if dim == "x" else "left"):
                                options[f"{dim}ticks_visible"]})
        if f"{dim}tick_color" in options:
            axis.tick_params(axis=dim, which="both", colors=options[f"{dim}tick_color"])
    if "aspect" in options:
        value = options["aspect"]
        value = value if value in ("auto", "equal") else float(value)
        if isinstance(value, float) and (not np.isfinite(value) or value <= 0):
            raise ValueError("軸の縦横比はauto/equalまたは正数で指定してください")
        axis.set_aspect(value, adjustable="box")
    bounds = list(axis.get_position().bounds)
    names = ("left", "bottom", "panel_width", "panel_height")
    if any(name in options for name in names):
        bounds = [float(options.get(name, current)) for name, current in zip(names, bounds)]
        if not np.isfinite(bounds).all() or min(bounds[:2]) < 0 or min(bounds[2:]) <= 0 \
                or bounds[0] + bounds[2] > 1 or bounds[1] + bounds[3] > 1:
            raise ValueError("パネル位置と寸法は図の0〜1の範囲内にしてください")
        axis.figure.set_layout_engine(None)
        axis.set_position(bounds)
    for text, horizontal, vertical in ((axis.title, "title_x", "title_y"),
                                      (axis.xaxis.label, "xlabel_x", None),
                                      (axis.yaxis.label, None, "ylabel_y")):
        if horizontal in options or vertical in options:
            x, y = text.get_position()
            text.set_position((options.get(horizontal, x), options.get(vertical, y)))
    if "title_y" in options:
        axis._autotitlepos = False
    for name, spine in axis.spines.items():
        for suffix in ("visible", "color"):
            key = f"spine_{name}_{suffix}"
            if key in options:
                getattr(spine, f"set_{suffix}")(options[key])
    for which in ("major", "minor"):
        prefix = f"grid_{which}"
        attributes = {name: options[f"{prefix}_{name}"]
                      for name in ("color", "linewidth", "linestyle", "alpha")
                      if f"{prefix}_{name}" in options}
        if prefix in options or attributes:
            visible = options.get(prefix, any(t.gridline.get_visible()
                                  for c in (axis.xaxis, axis.yaxis)
                                  for t in getattr(c, f"get_{which}_ticks")()))
            axis.grid(visible, which=which, **attributes)


def apply_style(figure: Figure, style: dict) -> None:
    """Apply presentation overrides without altering the plotted observations."""
    global_style = style.get("figure", {})
    width = float(global_style.get("width", figure.get_figwidth()))
    height = float(global_style.get("height", figure.get_figheight()))
    dpi = float(global_style.get("dpi", figure.dpi))
    if not (0.5 <= width <= 40 and 0.5 <= height <= 40 and 36 <= dpi <= 600):
        raise ValueError("図の幅・高さは0.5〜40 inch、DPIは36〜600で指定してください")
    if width * height * dpi ** 2 > 32_000_000:
        raise ValueError("画像が大きすぎます。幅・高さまたはDPIを下げてください（3200万画素まで）")
    figure.set_size_inches(width, height)
    figure.set_dpi(dpi)
    if "title" in global_style:
        figure.suptitle(global_style["title"])
    if "background" in global_style:
        figure.set_facecolor(global_style["background"])
    if "font_size" in global_style:
        from matplotlib.text import Text

        size = _validate_font_size(global_style["font_size"])
        for text in figure.findobj(Text):
            text.set_fontsize(size)
        for axis in figure.axes:
            axis.tick_params(labelsize=size)
    if "title_size" in global_style and figure._suptitle:
        figure._suptitle.set_fontsize(_validate_font_size(global_style["title_size"]))
    for key, options in style.get("axes", {}).items():
        axis = figure.axes[int(key)]
        if axis.name != "3d":
            _apply_2d(axis, options)
        for name in ("title", "xlabel", "ylabel", "zlabel"):
            if name in options:
                getattr(axis, f"set_{name}")(options[name])
        if "title_size" in options:
            axis.title.set_fontsize(_validate_font_size(options["title_size"]))
        if "label_size" in options:
            for name in ("xaxis", "yaxis", "zaxis"):
                if hasattr(axis, name):
                    getattr(axis, name).label.set_fontsize(_validate_font_size(options["label_size"]))
        if "tick_size" in options:
            axis.tick_params(labelsize=_validate_font_size(options["tick_size"]))
        for dim in ("x", "y"):
            step = options.get(f"{dim}tick_step")
            if step is not None:
                if getattr(axis, f"get_{dim}scale")() != "linear":
                    raise ValueError("目盛り間隔の指定は線形軸で使用してください")
                low, high = getattr(axis, f"get_{dim}lim")()
                low, high = options.get(f"{dim}min", low), options.get(f"{dim}max", high)
                if step <= 0 or abs(high - low) / step > 500:
                    raise ValueError("目盛り間隔は正数で、目盛り数が500以下になる値にしてください")
                getattr(axis, f"{dim}axis").set_major_locator(MultipleLocator(step))
            if f"{dim}tick_rotation" in options:
                axis.tick_params(axis=dim, labelrotation=options[f"{dim}tick_rotation"])
        for dim in ("x", "y", "z"):
            if f"{dim}min" in options or f"{dim}max" in options:
                low, high = getattr(axis, f"get_{dim}lim")()
                low, high = options.get(f"{dim}min", low), options.get(f"{dim}max", high)
                if not np.isfinite([low, high]).all() or low == high:
                    raise ValueError(f"{dim}limには異なる有限の値を指定してください")
                if getattr(axis, f"get_{dim}scale")() == "log" and min(low, high) <= 0:
                    raise ValueError(f"対数軸の{dim}limには正の値を指定してください")
                getattr(axis, f"set_{dim}lim")(low, high)
        if axis.name != "3d" and axis.get_adjustable() == "datalim" and any(
            name in options for name in ("xmin", "xmax", "ymin", "ymax")
        ):
            # axis('equal') otherwise rewrites the user's limits during savefig.
            axis.set_adjustable("box")
        if "grid" in options:
            axis.grid(options["grid"])
        if "elev" in options or "azim" in options:
            axis.view_init(elev=options.get("elev", axis.elev),
                           azim=options.get("azim", axis.azim))
        items = artists(axis)
        for index, changes in options.get("artists", {}).items():
            artist = items[int(index)]
            for name, value in changes.items():
                if name not in {"label", "visible", "alpha", "color", "linewidth", "linestyle",
                                "marker", "markersize", "markeredgewidth", "markerfacecolor",
                                "markeredgecolor", "facecolor", "edgecolor", "size", "text",
                                "fontsize", "position_x", "position_y"}:
                    raise ValueError(f"編集対象外の属性です: {name}")
                if name in ("position_x", "position_y"):
                    position = list(artist.get_position())
                    position[0 if name == "position_x" else 1] = float(value)
                    if not np.isfinite(position).all():
                        raise ValueError("注釈位置は有限数で指定してください")
                    artist.set_position(position)
                    continue
                if name == "fontsize":
                    value = _validate_font_size(value)
                if name in ("size", "markersize", "markeredgewidth", "linewidth", "fontsize") \
                        and float(value) < 0:
                    raise ValueError("サイズ・線幅・文字サイズには0以上を指定してください")
                if name == "size" and isinstance(artist, PathCollection):
                    artist.set_sizes([float(value)])
                elif name == "marker" and isinstance(artist, PathCollection):
                    marker = MarkerStyle(value)
                    artist.set_paths([marker.get_path().transformed(marker.get_transform())])
                elif name == "facecolor" and isinstance(artist, Collection):
                    # An explicit uniform color replaces scalar colormapping only by user choice.
                    artist.set_array(None)
                    artist.set_facecolor(value)
                else:
                    getattr(artist, f"set_{name}")(value)
        old_legend = axis.get_legend()
        if options.get("legend") is False:
            if old_legend:
                old_legend.set_visible(False)
        elif any(name in options for name in ("legend", "legend_location", "legend_size",
                                              "legend_columns", "legend_frame")) or (
            old_legend and options.get("artists")
        ):
            location = options.get("legend_location", old_legend._loc if old_legend else "best")
            location = int(location) if str(location).isdigit() else location
            size = options.get("legend_size", old_legend.get_texts()[0].get_fontsize()
                               if old_legend and old_legend.get_texts() else 10)
            if old_legend:
                # Preserve placement, frame and spacing when refreshing edited series handles.
                previous = old_legend
                kwargs = {"loc": location, "fontsize": _validate_font_size(size),
                          "ncols": int(options.get("legend_columns", previous._ncols)),
                          "frameon": options.get("legend_frame", previous.get_frame_on()),
                          "borderpad": previous.borderpad, "labelspacing": previous.labelspacing,
                          "handlelength": previous.handlelength, "handleheight": previous.handleheight,
                          "handletextpad": previous.handletextpad,
                          "borderaxespad": previous.borderaxespad,
                          "columnspacing": previous.columnspacing}
                if previous._bbox_to_anchor is not None:
                    anchor = previous.get_bbox_to_anchor().transformed(axis.transAxes.inverted())
                    kwargs.update(bbox_to_anchor=anchor, bbox_transform=axis.transAxes)
                handles, labels = axis.get_legend_handles_labels()
                if len(handles) == len(previous.get_texts()):
                    if not any("label" in change for change in options.get("artists", {}).values()):
                        labels = [text.get_text() for text in previous.get_texts()]
                else:
                    # Custom proxy legends can intentionally differ from the axes' artists.
                    handles = previous.legend_handles
                    labels = [text.get_text() for text in previous.get_texts()]
                refreshed = axis.legend(handles, labels, **kwargs)
                refreshed.get_frame().update_from(previous.get_frame())
                refreshed.set_frame_on(options.get("legend_frame", previous.get_frame_on()))
                refreshed.set_title(previous.get_title().get_text(),
                                    prop=previous.get_title().get_fontproperties())
                for current, old in zip(refreshed.get_texts(), previous.get_texts()):
                    current.set_fontproperties(old.get_fontproperties())
                    current.set_color(old.get_color())
                    if "legend_size" in options:
                        current.set_fontsize(_validate_font_size(size))
                refreshed.set_visible(options.get("legend", previous.get_visible()))
            else:
                axis.legend(loc=location, fontsize=_validate_font_size(size),
                            ncols=int(options.get("legend_columns", 1)),
                            frameon=options.get("legend_frame", True))
    if "font" in global_style:
        from matplotlib.text import Text

        try:
            findfont(FontProperties(family=[global_style["font"]]), fallback_to_default=False)
        except ValueError as error:
            raise ValueError("指定したフォントが見つかりません。このPCの候補から選んでください") from error
        for text in figure.findobj(Text):
            text.set_fontfamily(global_style["font"])


def _merge(target: dict, changes: dict) -> None:
    for key, value in changes.items():
        if isinstance(value, dict):
            _merge(target.setdefault(key, {}), value)
        else:
            target[key] = value


class PlotDocument:
    """One trusted-local master and independent, portable JSON overrides."""

    def __init__(self, master: Path):
        self.master = master.resolve()
        if not self.master.name.endswith(".figure.pickle"):
            raise ValueError("Select a locally generated .figure.pickle file")
        self.base = self.master.with_name(self.master.name.removesuffix(".figure.pickle"))
        self.style_path = self.base.with_name(self.base.name + ".style.json")
        self.original = self.master.read_bytes()
        metadata = self.base.with_name(self.base.name + ".plot.json")
        self.metadata = json.loads(metadata.read_text(encoding="utf-8")) if metadata.exists() else {}
        self.style = json.loads(self.style_path.read_text(encoding="utf-8")) \
            if self.style_path.exists() else {}
        self.figure = self._build(self.style)

    def _build(self, style: dict) -> Figure:
        # Only open files generated locally by save_editable_figure, never downloaded pickles.
        figure = pickle.loads(self.original)
        if not isinstance(figure, Figure):
            raise TypeError("Not a Matplotlib figure")
        # Unpickling pyplot figures can register managers; do not accumulate them per edit.
        from matplotlib import pyplot as plt

        plt.close(figure)
        # A closed Tk/Qt canvas may retain a destroyed window manager. Detach it
        # before set_dpi/set_size_inches, including when used from a Python REPL.
        FigureCanvasAgg(figure)
        original_dpi = self.metadata.get("savefig", {}).get("dpi")
        if isinstance(original_dpi, (int, float)):
            figure.set_dpi(original_dpi)
        apply_style(figure, style)
        return figure

    def update(self, changes: dict) -> None:
        candidate = copy.deepcopy(self.style)
        _merge(candidate, changes)
        figure = self._build(candidate)  # Invalid edits never replace a good figure/style.
        self.style, self.figure = candidate, figure

    def render(self, fmt: str) -> bytes:
        stream = io.BytesIO()
        options = self.metadata.get("savefig", {}).copy()
        options.pop("dpi", None)
        if any(key in self.style.get("figure", {}) for key in ("width", "height")):
            # Cropping would change the physical dimensions requested by the resize handles.
            options.pop("bbox_inches", None)
        if "background" in self.style.get("figure", {}):
            options["facecolor"] = self.style["figure"]["background"]
        context = {"svg.fonttype": "none"}
        if "font" in self.style.get("figure", {}):
            context["font.family"] = self.style["figure"]["font"]
        if "font_size" in self.style.get("figure", {}):
            context["font.size"] = self.style["figure"]["font_size"]
        with matplotlib.rc_context(context):
            self.figure.savefig(stream, format=fmt, dpi=self.figure.dpi, **options)
        return stream.getvalue()

    def save(self) -> None:
        _write_json(self.style_path, self.style)
        for fmt in ("png", "svg", "pdf"):
            path = self.base.with_name(self.base.name + f".edited.{fmt}")
            path.write_bytes(self.render(fmt))


def gallery_html(entries: list[dict], *, live: bool = False) -> str:
    """Build a bounded selection page; source files are never served as arbitrary paths."""
    cards = []
    for index, entry in enumerate(entries):
        preview = f"/gallery-preview/{index}.png" if live else entry["preview_relative"]
        button = (f'<button data-select="{index}">この図を編集</button>' if live else "")
        cards.append(f'<article><h2>{html.escape(entry["title"])}</h2>'
                     f'<p>{html.escape(entry["description"])}</p>'
                     f'<img src="{html.escape(preview, quote=True)}" alt="元の2Dグラフ" '
                     'width="720" height="420" loading="lazy">'
                     f'{button}<pre>{html.escape(entry["command"])}</pre></article>')
    script = """<script>const token='__TOKEN__';
document.querySelectorAll('[data-select]').forEach(button=>button.onclick=async()=>{
button.disabled=true;const response=await fetch('/select',{method:'POST',headers:{
'Content-Type':'application/json','X-Plot-Token':token},body:JSON.stringify({index:Number(button.dataset.select)})});
if(response.ok)location.href='/editor';else{document.getElementById('status').textContent=(await response.json()).error;button.disabled=false}
});</script>""" if live else ""
    return ('<!doctype html><html lang="ja"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>2D研究グラフ一覧</title><style>'
            'body{font:16px/1.6 "Yu Gothic",sans-serif;background:#f1f3f4;color:#202b30;'
            'margin:0;padding:24px}main{max-width:1100px;margin:auto}'
            'article{background:white;border:1px solid #b5c1c5;margin:24px 0;padding:20px}'
            'h1{font-size:26px}h2{font-size:20px}img{width:100%;height:auto;max-height:500px;'
            'object-fit:contain}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef3f4;'
            'padding:12px}button,a{font:inherit;min-height:44px;padding:8px;color:#166878}'
            ':focus-visible{outline:3px solid #eab15e}p{overflow-wrap:anywhere}</style>'
            '<main><h1>2D研究グラフ一覧</h1><p>元の図から編集を開始します。'
            '生成コード・計測値は保持し、保存した見た目は別ファイルに残します。'
            '一覧のサムネイルは生成時の図です。</p><p id="status" role="status"></p>'
            + ''.join(cards) + '</main>' + script + '</html>')


def make_server(document: PlotDocument, port: int = 0,
                idle_timeout: float = 1800, gallery: list[dict] | None = None) -> HTTPServer:
    if idle_timeout <= 0:
        raise ValueError("Idle timeout must be positive")
    token = secrets.token_urlsafe(24)
    html_path = Path(__file__).with_name("plot_editor.html")

    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            self.server.last_activity = time.monotonic()
            super().handle()

        def log_message(self, *args):
            pass

        def send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            try:
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, BrokenPipeError):
                pass  # A closed preview tab must not turn a completed save into a second response.

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/gallery") and gallery:
                self.send(gallery_html(gallery, live=True).replace("__TOKEN__", token).encode(),
                          "text/html; charset=utf-8")
            elif path.startswith("/gallery-preview/") and gallery:
                try:
                    index = int(path.removeprefix("/gallery-preview/").removesuffix(".png"))
                    if index < 0:
                        raise IndexError(index)
                    self.send(Path(gallery[index]["preview"]).read_bytes(), "image/png")
                except (ValueError, IndexError, OSError):
                    self.send(b"Not found", "text/plain", 404)
            elif path in ("/", "/editor"):
                page = html_path.read_text(encoding="utf-8")
                if gallery:
                    page = page.replace('<header>', '<header><a href="/gallery">2Dグラフ一覧に戻る</a>')
                example = ("from pathlib import Path\n"
                           "from aquabeacon_analysis.editable_plots import PlotDocument, save_editable_figure\n"
                           f"doc = PlotDocument(Path({str(document.master)!r}))\n"
                           "fig = doc.figure\n"
                           "# Matplotlibの全ArtistプロパティをPythonで編集できます。\n"
                           "# fig.axes[0].set_title('...')\n"
                           "save_editable_figure(fig, doc.base.with_name(doc.base.name + '.custom.png'))")
                page = page.replace("__PYTHON_EXAMPLE__", html.escape(example))
                self.send(page.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8")
            elif path == "/state":
                state = describe_figure(document.figure)
                state["figure"]["font_size"] = document.style.get("figure", {}).get("font_size")
                for axis_id, options in document.style.get("axes", {}).items():
                    for name in ("xtick_positions", "ytick_positions", "xtick_labels",
                                 "ytick_labels", "xminor_step", "yminor_step"):
                        if name in options:
                            state["axes"][int(axis_id)][name] = options[name]
                    for artist_id, changes in options.get("artists", {}).items():
                        if "marker" in changes:
                            state["axes"][int(axis_id)]["artists"][int(artist_id)]["marker"] = \
                                changes["marker"]
                self.send(json.dumps(state).encode(), "application/json")
            elif path == "/fonts":
                self.send(json.dumps(available_fonts()).encode(), "application/json")
            elif path in ("/figure.svg", "/figure.png", "/figure.pdf"):
                fmt = path.rsplit(".", 1)[1]
                mime = {"svg": "image/svg+xml", "png": "image/png", "pdf": "application/pdf"}
                self.send(document.render(fmt), mime[fmt])
            else:
                self.send(b"Not found", "text/plain", 404)

        def do_POST(self):
            nonlocal document
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1_000_000:
                    raise ValueError("Request too large")
                # Drain the bounded body before closing a rejected HTTP/1.0 request.
                # Unread request bytes can replace the 403 with a TCP reset on Windows.
                raw_payload = self.rfile.read(length)
                if self.headers.get("X-Plot-Token") != token:
                    self.send(b"Forbidden", "text/plain", 403)
                    return
                payload = json.loads(raw_payload or b"{}")
                if self.path == "/select" and gallery:
                    index = payload["index"]
                    if not isinstance(index, int) or index < 0 or index >= len(gallery):
                        raise ValueError("一覧内の図を選択してください")
                    candidate = PlotDocument(Path(gallery[index]["master"]))
                    if any(axis.name == "3d" for axis in candidate.figure.axes):
                        raise ValueError("この一覧は2D図だけを扱います")
                    document = candidate
                elif self.path == "/update":
                    document.update(payload)
                elif self.path == "/save":
                    document.save()
                elif self.path == "/reset":
                    document.figure = document._build({})
                    document.style = {}
                elif self.path == "/shutdown":
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif self.path == "/save-and-shutdown":
                    document.save()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    raise ValueError("Unknown operation")
                self.send(b'{"ok":true}', "application/json")
            except (ValueError, TypeError, AttributeError, IndexError, KeyError, OSError) as error:
                self.send(json.dumps({"error": str(error)}).encode(), "application/json", 400)

    class IdleServer(HTTPServer):
        def __init__(self):
            self.last_activity = time.monotonic()
            self.expiring = False
            super().__init__(("127.0.0.1", port), Handler)

        def service_actions(self):
            if not self.expiring and time.monotonic() - self.last_activity > idle_timeout:
                self.expiring = True
                threading.Thread(target=self.shutdown, daemon=True).start()

    return IdleServer()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master", type=Path, help="trusted, locally generated .figure.pickle")
    parser.add_argument("--port", type=int, default=0, help="localhost port (default: automatic)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--idle-timeout", type=float, default=1800,
                        help="stop after this many idle seconds (default: 1800)")
    args = parser.parse_args(argv)
    # The editor renders in the browser; never restore a Tk/Qt window from a pickle.
    matplotlib.use("Agg")
    server = make_server(PlotDocument(args.master), args.port, args.idle_timeout)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Plot editor: {url}\nPID {os.getpid()} started {datetime.now(UTC).isoformat()}"
          "\nStop with Ctrl+C after saving.", flush=True)
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
