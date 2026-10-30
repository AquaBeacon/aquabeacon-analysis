"""Verify that presentation editing keeps observations and saved overrides intact."""

import ast
import io
import json
import pickle
import re
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pytest
from matplotlib.text import Text
from PIL import Image

from aquabeacon_analysis.editable_plots import (
    PlotDocument,
    available_fonts,
    describe_figure,
    make_server,
    save_editable_figure,
)


@pytest.fixture
def master(tmp_path):
    figure, axis = plt.subplots()
    axis.plot([0, 1, 2], [1, np.nan, 3], label="Original", marker="o")
    axis.scatter([0, 2], [1, 3], s=[10, 30], c=[0, 1], label="Samples")
    axis.fill_between([0, 1, 2], [0, 0, 0], [1, 2, 3], alpha=0.2)
    axis.set(xlabel="Time (s)", ylabel="Distance (m)")
    axis.legend()
    save_editable_figure(figure, tmp_path / "sample.png", dpi=160)
    plt.close(figure)
    return tmp_path / "sample.figure.pickle"


def test_loader_detaches_destroyed_gui_manager_before_resizing(master, monkeypatch):
    close = plt.close

    class DestroyedWindow:
        def resize(self, *_):
            raise RuntimeError("GUI canvas has already been destroyed")

    def close_with_destroyed_manager(figure):
        close(figure)
        figure.canvas.manager = DestroyedWindow()

    monkeypatch.setattr(plt, "close", close_with_destroyed_manager)
    document = PlotDocument(master)
    document.update({"figure": {"width": 8}})
    assert document.figure.canvas.manager is None
    assert document.render("png").startswith(b"\x89PNG")


def test_round_trip_edits_preserve_data_and_missing_values(master):
    original_bytes = master.read_bytes()
    doc = PlotDocument(master)
    doc.update({"figure": {"width": 7, "font": "Yu Gothic"}, "axes": {"0": {
        "xlabel": "時間 (s)", "ylabel": "距離 (m)", "xtick_step": 0.5,
        "legend_location": "upper left", "legend_size": 12,
        "artists": {"0": {"label": "推定値", "linewidth": 3.5, "markersize": 8,
                          "color": "#206680"},
                    "1": {"size": 49, "marker": "s", "facecolor": "#cc5500",
                          "edgecolor": "black", "linewidth": 2, "alpha": 0.8},
                    "2": {"facecolor": "#99bbaa", "alpha": 0.3}},
    }}})
    doc.save()
    restored = PlotDocument(master)
    axis = restored.figure.axes[0]
    assert axis.get_xlabel() == "時間 (s)"
    assert axis.get_ylabel() == "距離 (m)"
    assert axis.lines[0].get_linewidth() == 3.5
    assert axis.lines[0].get_label() == "推定値"
    np.testing.assert_equal(axis.lines[0].get_ydata(), [1, np.nan, 3])
    np.testing.assert_equal(axis.collections[0].get_offsets(), [[0, 1], [2, 3]])
    np.testing.assert_equal(axis.collections[0].get_sizes(), [49])
    assert restored.figure.dpi == 160
    assert master.read_bytes() == original_bytes
    assert master.with_name("sample.edited.png").read_bytes().startswith(b"\x89PNG")
    svg = master.with_name("sample.edited.svg").read_text(encoding="utf-8")
    assert "時間 (s)" in svg
    assert "<text" in svg
    assert master.with_name("sample.edited.pdf").read_bytes().startswith(b"%PDF")
    assert not plt.get_fignums()


def test_invalid_edit_is_transactional_and_variable_colors_are_not_flattened(master):
    doc = PlotDocument(master)
    state = describe_figure(doc.figure)
    assert state["axes"][0]["artists"][1]["size"] is None
    assert doc.figure.axes[0].collections[0].get_array() is not None
    with pytest.raises(ValueError):
        doc.update({"axes": {"0": {"artists": {"0": {"color": "invalid-color"}}}}})
    assert doc.style == {}
    assert doc.figure.axes[0].get_xlabel() == "Time (s)"
    with pytest.raises(ValueError):
        doc.update({"figure": {"width": 40, "height": 40, "dpi": 600}})
    with pytest.raises(ValueError):
        doc.update({"axes": {"0": {"xtick_step": 0.000001}}})


def test_3d_scatter_and_annotations_are_editable(tmp_path):
    figure = plt.figure()
    axis = figure.add_subplot(projection="3d")
    axis.scatter([0, 1], [1, 2], [2, 3], s=3)
    axis.text(0, 1, 2, "Annotation")
    path = tmp_path / "map.png"
    save_editable_figure(figure, path)
    plt.close(figure)
    doc = PlotDocument(path.with_suffix(".figure.pickle"))
    doc.update({"axes": {"0": {"zlabel": "Depth (m)", "elev": 15, "azim": 25,
                               "artists": {"0": {"size": 20},
                                           "1": {"text": "Revised", "fontsize": 15}}}}})
    assert doc.figure.axes[0].get_zlabel() == "Depth (m)"
    assert doc.figure.axes[0].texts[0].get_text() == "Revised"
    assert b"<svg" in doc.render("svg")


def test_regeneration_keeps_user_style(master):
    doc = PlotDocument(master)
    doc.update({"axes": {"0": {"xlabel": "Elapsed (s)"}}})
    doc.save()
    figure = pickle.loads(master.read_bytes())
    save_editable_figure(figure, master.with_name("sample.png"))
    plt.close(figure)
    assert PlotDocument(master).figure.axes[0].get_xlabel() == "Elapsed (s)"


def test_resized_dimensions_and_axis_limits_survive_save(master):
    doc = PlotDocument(master)
    doc.metadata["savefig"]["bbox_inches"] = "tight"
    doc.update({"figure": {"width": 7, "height": 3, "dpi": 100},
                "axes": {"0": {"xmin": 0.2, "xmax": 1.8, "ymin": -2, "ymax": 5}}})
    assert Image.open(io.BytesIO(doc.render("png"))).size == (700, 300)
    doc.save()
    restored = PlotDocument(master)
    np.testing.assert_allclose(restored.figure.get_size_inches(), [7, 3])
    np.testing.assert_allclose(restored.figure.axes[0].get_xlim(), [0.2, 1.8])
    np.testing.assert_allclose(restored.figure.axes[0].get_ylim(), [-2, 5])
    np.testing.assert_equal(restored.figure.axes[0].lines[0].get_ydata(), [1, np.nan, 3])


@pytest.mark.parametrize("limits", [{"xmin": 1, "xmax": 1}, {"ymin": float("inf")}])
def test_invalid_axis_limits_leave_previous_view_unchanged(master, limits):
    doc = PlotDocument(master)
    before = doc.figure.axes[0].get_xlim(), doc.figure.axes[0].get_ylim()
    with pytest.raises(ValueError, match="lim"):
        doc.update({"axes": {"0": limits}})
    assert (doc.figure.axes[0].get_xlim(), doc.figure.axes[0].get_ylim()) == before


def test_equal_aspect_trajectory_keeps_explicit_limits_when_rendered(tmp_path):
    figure, axis = plt.subplots()
    axis.plot([0, 1, 2], [0, 1, 0])
    axis.axis("equal")
    save_editable_figure(figure, tmp_path / "trajectory.png")
    plt.close(figure)
    doc = PlotDocument(tmp_path / "trajectory.figure.pickle")
    doc.update({"axes": {"0": {"xmin": -5, "xmax": 5, "ymin": -1, "ymax": 2}}})
    doc.render("png")
    np.testing.assert_allclose(doc.figure.axes[0].get_xlim(), [-5, 5])
    np.testing.assert_allclose(doc.figure.axes[0].get_ylim(), [-1, 2])


def test_fonts_and_global_and_individual_sizes_survive_save(master):
    doc = PlotDocument(master)
    doc.update({"figure": {"font": "DejaVu Serif", "font_size": 18,
                           "title": "Figure title", "title_size": 26},
                "axes": {"0": {"title": "Panel title", "title_size": 24,
                               "label_size": 20, "tick_size": 12, "legend_size": 14}}})
    doc.save()
    restored = PlotDocument(master)
    restored.render("png")
    axis = restored.figure.axes[0]
    assert restored.figure._suptitle.get_fontsize() == 26
    assert axis.title.get_fontsize() == 24
    assert axis.xaxis.label.get_fontsize() == 20
    assert axis.yaxis.label.get_fontsize() == 20
    assert all(t.get_fontsize() == 12 for t in axis.get_xticklabels() + axis.get_yticklabels())
    assert all(t.get_fontsize() == 14 for t in axis.get_legend().get_texts())
    assert all(t.get_fontfamily() == ["DejaVu Serif"] for t in restored.figure.findobj(Text))
    np.testing.assert_equal(axis.lines[0].get_ydata(), [1, np.nan, 3])


def test_global_font_size_applies_to_titles_labels_ticks_and_legend(master):
    doc = PlotDocument(master)
    doc.update({"figure": {"font_size": 17}})
    doc.render("svg")
    axis = doc.figure.axes[0]
    assert axis.title.get_fontsize() == 17
    assert axis.xaxis.label.get_fontsize() == 17
    assert all(t.get_fontsize() == 17 for t in axis.get_xticklabels())
    assert all(t.get_fontsize() == 17 for t in axis.get_legend().get_texts())


@pytest.mark.parametrize("size", [0, -1, float("inf")])
def test_invalid_typography_does_not_replace_previous_style(master, size):
    doc = PlotDocument(master)
    with pytest.raises(ValueError, match="文字サイズ"):
        doc.update({"figure": {"font_size": size}})
    assert doc.style == {}
    with pytest.raises(ValueError, match="フォント"):
        doc.update({"figure": {"font": "AquaBeacon missing font 12345"}})
    assert doc.style == {}


def test_browser_api_round_trip_and_token(master):
    server = make_server(PlotDocument(master))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base, timeout=5) as response:
            html = response.read().decode()
        with urlopen(base + "/fonts", timeout=5) as response:
            fonts = json.load(response)
            assert fonts == available_fonts()
            assert "DejaVu Sans" in fonts
        token = re.search(r"const token='([^']+)'", html)[1]
        for unauthorized_body in (b"{}", b"{not-json}"):
            with pytest.raises(HTTPError) as rejected:
                urlopen(Request(base + "/save", data=unauthorized_body), timeout=5)
            assert rejected.value.code == 403
        assert not master.with_name("sample.style.json").exists()
        body = json.dumps({"axes": {"0": {"xlabel": "Browser edited"}}}).encode()
        for route, data in (("/update", body), ("/save", b"{}")):
            request = Request(base + route, data=data, headers={"X-Plot-Token": token})
            with urlopen(request, timeout=15) as response:
                assert json.load(response)["ok"]
        with urlopen(base + "/state", timeout=5) as response:
            assert json.load(response)["axes"][0]["xlabel"] == "Browser edited"
        for fmt in ("svg", "png", "pdf"):
            with urlopen(base + f"/figure.{fmt}", timeout=15) as response:
                assert response.status == 200
        assert PlotDocument(master).figure.axes[0].get_xlabel() == "Browser edited"
        request = Request(base + "/save-and-shutdown", data=b"{}",
                          headers={"X-Plot-Token": token})
        with urlopen(request, timeout=15) as response:
            assert json.load(response)["ok"]
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_analysis_plot_outputs_use_editable_saver():
    root = Path(__file__).resolve().parents[1]
    for folder in (root / "src/aquabeacon_analysis", root / "scripts"):
        for path in folder.glob("*.py"):
            if path.name == "editable_plots.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
            assert not any(isinstance(call.func, ast.Attribute) and call.func.attr == "savefig"
                           for call in calls), f"PNG-only output in {path}"


def test_initial_render_and_series_edit_preserve_baseline_layout_and_legend(tmp_path):
    figure, axis = plt.subplots(figsize=(6, 3))
    figure.subplots_adjust(right=.55)
    axis.plot([0, 1, 2], [1, np.nan, 3], label="A", color="red")
    axis.scatter([0, 1, 2], [1, 2, 3], c=[0, 1, 2], s=[5, 10, 20], label="Samples")
    axis.set_title("Baseline", fontsize=23, loc="left")
    axis.set_xlabel("X", fontsize=9)
    axis.set_aspect(1.2)
    axis.legend(loc="upper left", bbox_to_anchor=(1.05, 1), ncols=2,
                framealpha=.37, edgecolor="purple", handlelength=4)
    save_editable_figure(figure, tmp_path / "baseline.png", dpi=100, bbox_inches="tight")
    plt.close(figure)
    doc = PlotDocument(tmp_path / "baseline.figure.pickle")
    preview = np.asarray(Image.open(io.BytesIO(doc.render("png"))))
    original = np.asarray(Image.open(tmp_path / "baseline.png"))
    assert preview.shape == original.shape
    # Matplotlib constrained layout can shift subpixel antialiasing between PNG/SVG draws.
    assert np.mean(preview != original) < .002
    assert np.abs(preview.astype(float) - original).mean() < .1
    before = doc.figure.axes[0].get_legend()
    anchor = before.get_bbox_to_anchor().bounds
    doc.update({"axes": {"0": {"artists": {"0": {"linewidth": 4}}}}})
    axis = doc.figure.axes[0]
    legend = axis.get_legend()
    assert legend._ncols == 2
    assert legend.handlelength == 4
    assert axis.get_aspect() == 1.2
    assert legend.get_frame().get_alpha() == .37
    np.testing.assert_allclose(legend.get_bbox_to_anchor().bounds, anchor)
    np.testing.assert_equal(axis.collections[0].get_array(), [0, 1, 2])
    np.testing.assert_equal(axis.collections[0].get_sizes(), [5, 10, 20])
    assert axis.get_title(loc="left") == "Baseline"


def test_extended_2d_controls_round_trip_and_reject_data_mutation(master):
    doc = PlotDocument(master)
    doc.update({"axes": {"0": {
        "xscale": "symlog", "aspect": "equal", "left": .2,
        "bottom": .2, "panel_width": .6, "panel_height": .6,
        "ytick_positions": "1,2,3", "ytick_labels": "low|middle|high",
        "yminor_step": .25, "grid_minor": True, "grid_minor_color": "red",
        "grid_minor_alpha": .3, "spine_top_visible": False,
        "spine_left_color": "blue", "xticks_visible": False,
        "legend_columns": 2, "legend_frame": False, "title_x": .3,
        "artists": {"0": {"linewidth": 2}},
    }}})
    doc.save()
    restored = PlotDocument(master)
    axis = restored.figure.axes[0]
    assert axis.get_xscale() == "symlog"
    assert axis.get_aspect() == 1
    assert axis.get_legend()._ncols == 2
    assert not axis.get_legend().get_frame_on()
    assert not axis.spines["top"].get_visible()
    assert [text.get_text() for text in axis.get_yticklabels()] == ["low", "middle", "high"]
    assert axis.title.get_position()[0] == .3
    np.testing.assert_equal(axis.lines[0].get_ydata(), [1, np.nan, 3])
    before = restored.style.copy()
    with pytest.raises(ValueError, match="編集対象外"):
        restored.update({"axes": {"0": {"artists": {"0": {"ydata": [8, 9]}}}}})
    assert restored.style == before


def test_log_scale_requires_explicit_positive_limits(master):
    doc = PlotDocument(master)
    with pytest.raises(ValueError, match="正の表示範囲"):
        doc.update({"axes": {"0": {"xscale": "log"}}})
    doc.update({"axes": {"0": {"xscale": "log", "xmin": .1, "xmax": 10}}})
    np.testing.assert_allclose(doc.figure.axes[0].get_xlim(), [.1, 10])
    np.testing.assert_equal(doc.figure.axes[0].lines[0].get_xdata(), [0, 1, 2])


def test_editor_stops_after_idle_timeout(master):
    server = make_server(PlotDocument(master), idle_timeout=.05)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join(timeout=3)
        assert not thread.is_alive()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_gallery_selection_is_allowlisted_and_serves_only_named_previews(master):
    from aquabeacon_analysis.editable_plots import gallery_html

    entry = {"title": "Sample <figure>", "description": "Measurement plot",
             "command": "python -m aquabeacon_analysis.editable_plots sample.figure.pickle",
             "master": str(master), "preview": str(master.with_name("sample.png")),
             "preview_relative": "../sample.png"}
    assert "Sample &lt;figure&gt;" in gallery_html([entry])
    server = make_server(PlotDocument(master), gallery=[entry])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base, timeout=5) as response:
            page = response.read().decode()
        token = re.search(r"const token='([^']+)'", page)[1]
        assert 'data-select="0"' in page
        with urlopen(base + "/gallery-preview/0.png", timeout=5) as response:
            assert response.read().startswith(b"\x89PNG")
        for selection in [-1, 1, "../../other.figure.pickle"]:
            request = Request(base + "/select", data=json.dumps({"index": selection}).encode(),
                              headers={"X-Plot-Token": token})
            with pytest.raises(HTTPError) as rejected:
                urlopen(request, timeout=5)
            assert rejected.value.code == 400
        request = Request(base + "/select", data=b'{"index":0}',
                          headers={"X-Plot-Token": token})
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["ok"]
        with urlopen(base + "/editor", timeout=5) as response:
            assert '/gallery' in response.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_experiment_gallery_rejects_paths_and_unregistered_ids():
    import runpy

    module = runpy.run_path(str(Path(__file__).resolve().parents[1]
                                / "scripts/open_ngt_research_figures.py"))
    for experiment in ("../20261028_ngt_000", "C:/other", "unregistered_test_experiment"):
        with pytest.raises(ValueError):
            module["collect_figures"](experiment)
