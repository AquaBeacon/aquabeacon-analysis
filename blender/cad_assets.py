"""Reusable Blender loader. Each returned root has the corresponding body frame in meters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import bpy


def load_cad_body(bundle: Path, body: str, *, parent=None):
    """Import one CAD body; caller can keyframe its root using T_world_body.

    This function consumes only the frozen GLB, not the live mechanical repository.
    Child meshes keep their local placement under the returned body root.
    """
    manifest = json.loads((bundle / "assets.json").read_text(encoding="utf-8"))
    path = bundle / manifest["assets"][body]["file"]
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != manifest["assets"][body]["sha256"]:
        raise ValueError(f"CAD asset hash mismatch: {path}")
    profile_path = bundle / "rig.json"
    if hashlib.sha256(profile_path.read_bytes()).hexdigest() != manifest["profile_sha256"]:
        raise ValueError("CAD profile hash mismatch")
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    imported = set(bpy.data.objects) - before
    root = bpy.data.objects.new(f"{body}_body", None)
    bpy.context.scene.collection.objects.link(root)
    for obj in imported:
        if obj.parent not in imported:
            obj.parent = root
    root.parent = parent
    root["rig_id"] = manifest["rig_id"]
    root["body_frame"] = f"{body}_body"
    root["geometry_units"] = "m"
    root["extrinsics_status"] = manifest["status"]
    return root, imported
