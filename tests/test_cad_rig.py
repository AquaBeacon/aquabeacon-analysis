from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import numpy as np
import pytest

from aquabeacon_analysis.cad_rig import (
    anchor_alignment,
    export_assets,
    in_meters,
    placement_matrix,
    safe_id,
    scad_literal,
    sensor_transform,
    sha256,
    verify_sources,
)


def test_op_order_and_scad_euler_angles():
    transform = placement_matrix([{"translate": [10, 20, 30]}, {"rotate": [90, 0, 90]}])
    # X90 maps +Y to +Z, then Z90 keeps +Z unchanged, then translation.
    assert np.allclose(transform @ [0, 1, 0, 1], [10, 20, 31, 1])
    assert np.allclose(transform @ [1, 0, 0, 1], [10, 21, 30, 1])
    assert np.allclose(np.array(in_meters(transform))[:3, 3], [.01, .02, .03])


def test_horizontal_alignment_uses_shared_part_not_screenshots():
    source = {"items": [{"label": "tube", "part": "parts/tube", "ops": [
        {"rotate": [0, -90, 0]}, {"translate": [0, -57.15, -57.15]}]}]}
    target = json.loads(json.dumps(source))
    target["items"][0]["file"] = "tube"  # explicit and implicit default are equivalent
    target["items"][0]["ops"] = [
        {"translate": [0, -100, 138]}, {"rotate": [-90, 0, 0]},
        *source["items"][0]["ops"],
    ]
    alignment = anchor_alignment(target, source, "tube")
    assert np.allclose(alignment @ [0, 0, 0, 1], [0, -100, 138, 1])
    assert np.allclose(alignment @ [0, 0, 200, 1], [0, 100, 138, 1])
    target["items"][0]["part"] = "other_tube"
    with pytest.raises(ValueError, match="anchor sources differ"):
        anchor_alignment(target, source, "tube")


def test_missing_centers_cannot_be_used_as_lever_arms():
    sensor = {"id": "imu", "T_body_sensor": None, "as_built_verified": False,
              "measurement_axes_verified": False, "limitation": "envelope only"}
    profile = {"sensors": [sensor]}
    with pytest.raises(ValueError, match="measured extrinsic unavailable"):
        sensor_transform(profile, "imu")
    sensor.update(T_body_sensor=np.eye(4).tolist(), as_built_verified=True,
                  measurement_axes_verified=True)
    assert np.allclose(sensor_transform(profile, "imu"), np.eye(4))
    sensor["T_body_sensor"][0][0] = -1
    with pytest.raises(ValueError, match="invalid measured SE"):
        sensor_transform(profile, "imu")


@pytest.mark.parametrize("ops", [[{"mirror": [0, 0, 0]}], [{"scale": [1, 0, 1]}],
                                 [{"rotate": [float("nan"), 0, 0]}], [{"unknown": [0, 0, 0]}]])
def test_bad_transforms_fail(ops):
    with pytest.raises(ValueError):
        placement_matrix(ops)


@pytest.mark.parametrize("value", ["../old_experiment", "x/y", ".", "", "a\\b"])
def test_id_cannot_escape_output_root(value):
    with pytest.raises(ValueError):
        safe_id(value)


def test_glb_units_mirror_winding_and_instancing(tmp_path: Path):
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("PIL")
    mesh = trimesh.creation.box(extents=[10, 20, 30])
    mesh.apply_translation([10, 0, 0])
    mesh.export(tmp_path / "test.stl")
    components = []
    for name, ops in [("a", [{"translate": [100, 0, 0]}]),
                      ("b", [{"translate": [200, 0, 0]}]),
                      ("c", [{"translate": [300, 0, 0]}, {"mirror": [1, 0, 0]}])]:
        components.append({"id": name, "body": "parent", "mesh_source": "test.stl",
                           "color": "Orange", "alpha": 1.0,
                           "T_body_component": in_meters(placement_matrix(ops))})
    profile = {"bodies": {"parent": {}}, "components": components}
    assets = export_assets(profile, tmp_path, tmp_path / "out")
    assert assets["parent"]["nodes"] == 3
    assert assets["parent"]["geometry_count"] == 2
    scene = trimesh.load(tmp_path / "out/parent.glb", force="scene")
    assert np.allclose(scene.bounds, [[.105, -.015, -.01], [.295, .015, .01]])
    for geometry in scene.geometry.values():
        assert geometry.volume > 0  # reflected triangle winding remains outward


def test_frozen_profile_sensor_references_and_body_frames():
    path = Path(__file__).resolve().parents[1] / "calibration/rigs/cad_20261027.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    assert profile["scope"] == "future_experiments_only"
    alignments = profile["assembly_alignments"]
    assert np.allclose(np.array(alignments["parent_hand_service"])[:3, 3], [-.105, -.115, 0])
    assert np.allclose(np.array(alignments["child_interior"])[:3, 3], [0, -.100, .138])
    sensors = {s["id"]: s for s in profile["sensors"]}
    expected = {
        "parent_spresense": [.09734, .093, .0672],
        "parent_magnetometer": [-.05111, .0927, .072],
        "child_imu": [.029, -.056, .1334],
        "parent_td04a": [.053, -.055, -.085],
        "child_td04a": [.154, -.074, .173],
    }
    for name, position in expected.items():
        assert np.allclose(sensors[name]["reference_position_body_m"], position)
    assert len(sensors) == 9
    assert all(s["T_body_sensor"] is None for s in sensors.values())
    assert sensors["child_magnetometer"]["reference_position_body_m"] == (
        sensors["child_imu"]["reference_position_body_m"]
    )


def test_changed_cad_or_pcb_cannot_reuse_frozen_rig(tmp_path: Path):
    mechanical = tmp_path / "mechanical"
    mechanical.mkdir()
    cad = mechanical / "part.stl"
    cad.write_bytes(b"original mesh")
    analysis = tmp_path / "analysis"
    analysis.mkdir()
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_bytes(b"original board")
    profile = {"sources": {"part.stl": {"sha256": sha256(cad)}}, "sensors": [
        {"id": "child_imu", "pcb_source": {"path": "../board.kicad_pcb", "sha256": sha256(pcb)}}
    ]}
    verify_sources(profile, mechanical, analysis)
    cad.write_bytes(b"modified mesh")
    with pytest.raises(ValueError, match="CAD source changed"):
        verify_sources(profile, mechanical, analysis)
    cad.write_bytes(b"original mesh")
    pcb.write_bytes(b"modified board")
    with pytest.raises(ValueError, match="PCB source changed"):
        verify_sources(profile, mechanical, analysis)


def test_scad_dimensions_update_or_require_explicit_review(tmp_path: Path):
    scad = tmp_path / "bracket.scad"
    scad.write_text("sensor_offset=72; // updated CAD\n", encoding="utf-8")
    assert scad_literal(scad, "sensor_offset") == 72
    scad.write_text("sensor_offset=wall+60;", encoding="utf-8")
    with pytest.raises(ValueError, match="expected literal CAD parameter"):
        scad_literal(scad, "sensor_offset")


def test_future_binding_snapshots_profile_and_refuses_overwrite(tmp_path: Path, monkeypatch):
    from aquabeacon_analysis.cad_rig import write_json

    script = Path(__file__).resolve().parents[1] / "scripts/prepare_cad_rig.py"
    main = runpy.run_path(str(script))["main"]
    root = tmp_path / "analysis"
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_bytes(b"pcb")
    profile = {"rig_id": "future_rig", "scope": "future_experiments_only",
               "status": "cad_nominal_not_measured_extrinsics", "components": [],
               "sources": {}, "sensors": [{"id": "child_imu", "pcb_source": {
                   "path": "../board.kicad_pcb", "sha256": sha256(pcb)
               }}]}
    profile_path = root / "calibration/rigs/future_rig.json"
    write_json(profile_path, profile)
    monkeypatch.setitem(main.__globals__, "ROOT", root)
    monkeypatch.setattr(sys, "argv", [str(script), "--rig-id", "future_rig",
                                      "--bind-experiment", "FUTURE_SESSION"])
    main()
    output = root / "results/FUTURE_SESSION/cad_geometry"
    assert (output / "rig.json").read_bytes() == profile_path.read_bytes()
    assert json.loads((output / "manifest.json").read_text())["automatic_corrections_applied"] is False
    with pytest.raises(SystemExit):
        main()
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(sys, "argv", [str(script), "--rig-id", "future_rig", "--verify"])
    main()
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
