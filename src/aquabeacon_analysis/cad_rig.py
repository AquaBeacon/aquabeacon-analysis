"""Read-only CAD snapshots for future experiments; never infer measured extrinsics."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ASSEMBLIES = {
    "parent_hand_service": "parent",
    "parent_exterior": "parent",
    "child_horizontal_plate_frame": "child",
    "child_interior": "child",
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def placement_matrix(ops: list[dict]) -> np.ndarray:
    """OpenSCAD outer-first ops; Rz @ Ry @ Rx for rotate([x,y,z]). Units: mm."""
    total = np.eye(4)
    for operation in ops:
        if len(operation) != 1:
            raise ValueError("one transform per operation required")
        kind, values = next(iter(operation.items()))
        vector = np.asarray(values, dtype=float)
        if vector.shape != (3,) or not np.isfinite(vector).all():
            raise ValueError(f"invalid {kind} vector")
        transform = np.eye(4)
        if kind == "translate":
            transform[:3, 3] = vector
        elif kind == "rotate":
            for axis, angle in enumerate(np.radians(vector)):
                c, s = math.cos(angle), math.sin(angle)
                rotation = np.eye(4)
                i, j = [(1, 2), (2, 0), (0, 1)][axis]
                rotation[i, i] = rotation[j, j] = c
                rotation[i, j], rotation[j, i] = -s, s
                transform = rotation @ transform
        elif kind == "mirror":
            norm = vector @ vector
            if norm == 0:
                raise ValueError("zero mirror normal")
            transform[:3, :3] -= 2 * np.outer(vector, vector) / norm
        elif kind == "scale":
            if (vector == 0).any():
                raise ValueError("zero scale")
            transform[:3, :3] = np.diag(vector)
        else:
            raise ValueError(f"unsupported CAD transform: {kind}")
        total = total @ transform
    return total


def in_meters(matrix_mm: np.ndarray) -> list[list[float]]:
    result = matrix_mm.copy()
    result[:3, 3] *= 0.001
    return np.round(result, 12).tolist()


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value):
        raise ValueError("ID must contain only letters, digits, underscores and hyphens")
    return value


def _item(layout: dict, label: str) -> dict:
    matches = [item for item in layout["items"] if item["label"] == label]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one CAD item: {label}")
    return matches[0]


def scad_literal(path: Path, name: str) -> float:
    """Read a literal dimension only; expressions require an extractor review."""
    match = re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s*;",
                      path.read_text(encoding="utf-8-sig"))
    if not match:
        raise ValueError(f"expected literal CAD parameter: {name}: {path}")
    return float(match[1])


def anchor_alignment(target: dict, source: dict, label: str) -> np.ndarray:
    """Align two assemblies using the SAME vendor part in the SAME local frame."""
    a, b = _item(target, label), _item(source, label)
    if (a["part"], a.get("file", Path(a["part"]).name)) != (
        b["part"], b.get("file", Path(b["part"]).name)
    ):
        raise ValueError(f"anchor sources differ: {label}")
    transform = placement_matrix(a.get("ops", [])) @ np.linalg.inv(
        placement_matrix(b.get("ops", []))
    )
    if not np.allclose(transform[:3, :3].T @ transform[:3, :3], np.eye(3)):
        raise ValueError("body alignment must be rigid")
    if not np.isclose(np.linalg.det(transform[:3, :3]), 1):
        raise ValueError("body alignment must preserve handedness")
    return transform


def _git_state(root: Path) -> dict:
    def git(*args):
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    return {"commit": git("rev-parse", "HEAD"), "status": git("status", "--porcelain")}


def build_profile(mechanical_root: Path, rig_id: str, analysis_root: Path) -> dict:
    """Extract nominal references. Source files are only read, never regenerated."""
    import trimesh

    safe_id(rig_id)
    mechanical_root = mechanical_root.resolve()
    layouts, sources = {}, {}

    def record(relative: str) -> Path:
        path = (mechanical_root / relative).resolve()
        if not path.is_relative_to(mechanical_root):
            raise ValueError(f"source escapes mechanical root: {relative}")
        sources[relative] = {"sha256": sha256(path), "size_bytes": path.stat().st_size}
        return path

    for name in ASSEMBLIES:
        layouts[name] = json.loads(record(f"assemblies/{name}/placement.json").read_text(
            encoding="utf-8-sig"
        ))
        record(f"assemblies/{name}/README.md")
    alignments = {name: np.eye(4) for name in ASSEMBLIES}
    alignments["parent_hand_service"] = anchor_alignment(
        layouts["parent_exterior"], layouts["parent_hand_service"], "box_bcas212310g"
    )
    alignments["child_interior"] = anchor_alignment(
        layouts["child_horizontal_plate_frame"], layouts["child_interior"], "acrylic_tube_200"
    )
    components, omitted = [], []
    for assembly, body in ASSEMBLIES.items():
        labels = set()
        for item in layouts[assembly]["items"]:
            label = item["label"]
            if label in labels:
                raise ValueError(f"duplicate CAD label: {assembly}/{label}")
            labels.add(label)
            reason = None
            # Both interiors contain their own vessel. The exterior supplies it once.
            if (assembly in ("parent_hand_service", "child_interior")
                    and item.get("group") != "interior"):
                reason = "vessel already supplied by exterior"
            if any(token in label.lower() for token in ("keepout", "reservation", "sweep")):
                reason = "construction or clearance envelope"
            if reason:
                omitted.append({"id": f"{assembly}/{label}", "reason": reason})
                continue
            part = Path(item["part"])
            relative = (part / (item.get("file", part.name) + ".stl")).as_posix()
            record(relative)
            matrix = alignments[assembly] @ placement_matrix(item.get("ops", []))
            components.append({
                "id": f"{assembly}/{label}", "body": body, "assembly": assembly,
                "label": label, "group": item.get("group", "unspecified"),
                "mesh_source": relative, "color": item.get("color", "LightGray"),
                "alpha": item.get("alpha", 1.0),
                "T_body_component": in_meters(matrix),
                "note": item.get("note"),
            })

    def reference(body, assembly, label, name, point, kind, limitation, **extra):
        component_id = f"{assembly}/{label}"
        component = next(c for c in components if c["id"] == component_id)
        matrix = np.asarray(component["T_body_component"])
        position = (matrix @ np.r_[np.asarray(point) * .001, 1])[:3].tolist()
        return {
            "id": name, "body": body, "component_id": component_id,
            "reference_kind": kind, "reference_local_mm": point,
            "reference_position_body_m": position,
            "T_body_component": component["T_body_component"],
            "T_body_sensor": None, "measurement_center_body_m": None,
            "measurement_axes_verified": False, "as_built_verified": False,
            "limitation": limitation, **extra,
        }

    sensors = []
    for body, assembly, label in (
        ("parent", "parent_exterior", "camera_case_reference"),
        ("child", "child_horizontal_plate_frame", "side_camera_case_reference"),
    ):
        sensors.append(reference(
            body, assembly, label, f"{body}_camera", [0, 0, 0], "case_model_origin",
            "Case depth 41 mm measured; width, height and M5-axis offset provisional. "
            "Optical center, optical axes and camera internal IMU offset are unknown.",
        ))
    sensors.append(reference(
        "parent", "parent_hand_service", "spresense", "parent_spresense", [0, 0, 0],
        "extension_board_model_origin",
        "Sony main/extension STEP geometry; mating height provisional. Board origin is not IMU.",
    ))
    record("parts/parent/interior/cots/spresense_service_stack/sources.json")
    record("parts/parent/interior/cots/spresense_service_stack/spresense_service_stack.md")
    record("spec/build_parent_service_cad.py")
    envelope = trimesh.load_mesh(record(
        "parts/parent/interior/cots/spresense_service_stack/imu_nominal_envelope.stl"
    ), process=False)
    envelope_center = envelope.bounds.mean(axis=0).tolist()
    sensors.append(reference(
        "parent", "parent_hand_service", "spresense", "parent_imu", envelope_center,
        "nominal_imu_envelope_center",
        "Center of 20.6 x 50 x 9.1 mm envelope at [11.29,0,9.9]; "
        "not the multi-IMU sensing centers or measurement axes.",
    ))
    eagle_path = "parts/parent/interior/cots/adafruit_lis3mdl/ref/Adafruit LIS3MDL.brd"
    eagle = ET.parse(record(eagle_path))
    chips = [e for e in eagle.findall(".//board/elements/element")
             if e.get("value") == "LIS3MDL"]
    if len(chips) != 1:
        raise ValueError("expected one LIS3MDL in vendor EAGLE board")
    chip = chips[0]
    # XY is known. Z=0 is explicitly a projection onto the PCB underside datum.
    sensors.append(reference(
        "parent", "parent_hand_service", "magnetometer", "parent_magnetometer",
        [float(chip.get("x")), float(chip.get("y")), 0], "chip_xy_on_pcb_datum",
        "Vendor EAGLE IC1 XY is known; displayed point is projected to PCB underside. "
        "Die height, package-to-measurement-axis rotation and as-built orientation unverified.",
        chip_placement_rotation_eagle=chip.get("rot"), chip_source=eagle_path,
    ))
    # The mechanical child-main mesh uses a nominal cuboid BNO module. Locate the
    # carrier footprint from the actual PCB, not its outdated mechanical README.
    pcb = analysis_root.parent / "hardware/pcb/CHILD_MAIN/CHILD_MAIN.kicad_pcb"
    pcb_text = pcb.read_text(encoding="utf-8")
    match = re.search(
        r'\(footprint "Adafruit_BNO085_4754"\s*\(layer "F.Cu"\)\s*'
        r'\(uuid "[^"]+"\)\s*\(at ([\d.+-]+) ([\d.+-]+)(?: ([\d.+-]+))?\)',
        pcb_text,
    )
    if not match:
        raise ValueError("BNO085 carrier footprint not found; inspect PCB before updating extractor")
    pcb_source = {"path": "../hardware/pcb/CHILD_MAIN/CHILD_MAIN.kicad_pcb",
                  "sha256": sha256(pcb), "footprint": "J_BNO1",
                  "kicad_at_mm_deg": [float(v or 0) for v in match.groups()]}
    record("parts/child/interior/pcb/child_main/child_main.md")
    for name in ("child_imu", "child_magnetometer"):
        sensors.append(reference(
            "child", "child_interior", "child_main", name,
            [float(match[1]), -float(match[2]), 1.6], "bno085_carrier_footprint_at_pcb_top",
            "J_BNO1 footprint projected onto C-MAIN top; not the BNO085 chip/die center. "
            "Module/socket height and measurement axes require verification. "
            "IMU and magnetic observations belong to the same BNO085 module.",
            pcb_source=pcb_source,
        ))
    bracket = record("parts/common/exterior/printed/td04a_bracket_mount/td04a_bracket_mount.scad")
    offset = scad_literal(bracket, "sensor_offset")
    child_sensor_x = scad_literal(bracket, "child_sensor_x")
    record("parts/common/exterior/printed/td04a_bracket_mount/README.md")
    interface = record("scad/lib/td04a_interface.scad")
    pitch = scad_literal(interface, "td04a_mount_pitch")
    for body, assembly, label, point, normal in (
        ("parent", "parent_exterior", "td04a_r6_parent_strap", [0, 0, -offset], [0, 0, -1]),
        ("child", "child_horizontal_plate_frame", "td04a_r6_child_strap",
         [child_sensor_x, 0, offset], [0, 0, 1]),
    ):
        sensors.append(reference(
            body, assembly, label, f"{body}_td04a", point, "mount_hole_row_midpoint",
            f"Rev.6 bracket with provisional {pitch:g} mm pitch. TD04A body intentionally omitted "
            "from source CAD; acoustic phase center, ear thickness and offset unknown.",
            intended_face_normal_body=normal,
        ))
    return {
        "schema_version": 1, "rig_id": rig_id,
        "created_utc": datetime.now(UTC).isoformat(),
        "scope": "future_experiments_only", "status": "cad_nominal_not_measured_extrinsics",
        "transform_convention": "p_body = T_body_component @ p_component; columns; meters",
        "mesh_source_units": "mm", "body_units": "m", "source_root_hint": "../mechanical",
        "analysis_git": _git_state(analysis_root), "mechanical_git": _git_state(mechanical_root),
        "bodies": {
            "parent": {"frame": "parent_body", "origin": "box bottom center",
                       "axes": "+X,+Y as parent_exterior; +Z up"},
            "child": {"frame": "child_body", "origin": "horizontal frame CAD origin",
                      "axes": "+X right; +Y tube dome direction; +Z up; tube axis z=0.138 m"},
        },
        "assembly_alignments": {name: in_meters(t) for name, t in alignments.items()},
        "alignment_evidence": {
            "parent_hand_service": "identical box_bcas212310g source and local frame",
            "child_interior": "identical acrylic_tube_200 source and local frame; "
                              "nominal assembly alignment, physical tube roll unverified",
        },
        "sources": sources, "components": components, "omitted_components": omitted,
        "sensors": sensors,
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def verify_sources(profile: dict, mechanical_root: Path, analysis_root: Path) -> None:
    for relative, source in profile["sources"].items():
        if sha256(mechanical_root / relative) != source["sha256"]:
            raise ValueError(f"CAD source changed; create a new rig snapshot: {relative}")
    pcb = next(s["pcb_source"] for s in profile["sensors"] if s["id"] == "child_imu")
    if sha256(analysis_root / pcb["path"]) != pcb["sha256"]:
        raise ValueError("child PCB source changed; create a new rig snapshot")


def sensor_transform(profile: dict, sensor_id: str) -> np.ndarray:
    """Only return a complete, verified measured transform; never a CAD fallback."""
    sensor = next(s for s in profile["sensors"] if s["id"] == sensor_id)
    value = sensor["T_body_sensor"]
    if value is None or not sensor["measurement_axes_verified"] or not sensor["as_built_verified"]:
        raise ValueError(f"measured extrinsic unavailable: {sensor_id}: {sensor['limitation']}")
    matrix = np.asarray(value, dtype=float)
    if (matrix.shape != (4, 4) or not np.isfinite(matrix).all()
            or not np.allclose(matrix[3], [0, 0, 0, 1])
            or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3))
            or not np.isclose(np.linalg.det(matrix[:3, :3]), 1)):
        raise ValueError(f"invalid measured SE(3): {sensor_id}")
    return matrix


def export_assets(profile: dict, mechanical_root: Path, output: Path) -> dict:
    """Meter-scale GLBs with named nodes and shared geometry, usable outside Blender."""
    import trimesh
    from PIL import ImageColor

    output.mkdir(parents=True, exist_ok=True)
    assets = {}
    for body in profile["bodies"]:
        scene = trimesh.Scene()
        cached = {}
        # glTF is Y-up; CAD is Z-up. Blender's standard glTF importer reverses
        # this basis change, yielding the original CAD body frame in Blender.
        to_gltf = placement_matrix([{"rotate": [-90, 0, 0]}])
        for component in profile["components"]:
            if component["body"] != body:
                continue
            key = (component["mesh_source"], component["color"], component["alpha"])
            transform = np.asarray(component["T_body_component"])
            # glTF TRS nodes cannot reliably represent reflections. Bake the linear
            # reflection into a distinct mesh and reverse winding via apply_transform.
            reflected = np.linalg.det(transform[:3, :3]) < 0
            cache_key = (*key, tuple(transform[:3, :3].ravel()) if reflected else ())
            if cache_key not in cached:
                mesh = trimesh.load_mesh(mechanical_root / key[0], process=False)
                mesh.apply_scale(.001)
                if reflected:
                    linear = np.eye(4)
                    linear[:3, :3] = transform[:3, :3]
                    mesh.apply_transform(linear)
                rgba = [*ImageColor.getrgb(key[1]), round(255 * key[2])]
                mesh.visual = trimesh.visual.TextureVisuals(
                    material=trimesh.visual.material.PBRMaterial(
                        name=f"CAD_{key[1]}_{key[2]}", baseColorFactor=rgba,
                        metallicFactor=0.0, roughnessFactor=.65,
                        alphaMode="BLEND" if key[2] < 1 else "OPAQUE",
                    )
                )
                geometry_name = f"mesh_{len(cached):04d}"
                scene.geometry[geometry_name] = mesh
                cached[cache_key] = geometry_name
            if reflected:
                transform = transform.copy()
                transform[:3, :3] = np.eye(3)
            scene.graph.update(frame_to=component["id"], frame_from="world",
                               matrix=to_gltf @ transform, geometry=cached[cache_key])
        path = output / f"{body}.glb"
        scene.export(path)
        bounds_body = trimesh.transform_points(
            trimesh.bounds.corners(scene.bounds), np.linalg.inv(to_gltf)
        )
        assets[body] = {"file": path.name, "sha256": sha256(path),
                        "bounds_body_m": [bounds_body.min(axis=0).tolist(),
                                          bounds_body.max(axis=0).tolist()],
                        "bounds_gltf_m": scene.bounds.tolist(),
                        "gltf_axes": "Y up; gltf_xyz = [body_x, body_z, -body_y]",
                        "nodes": len(scene.graph.nodes_geometry),
                        "geometry_count": len(scene.geometry)}
        del scene, cached
    return assets
