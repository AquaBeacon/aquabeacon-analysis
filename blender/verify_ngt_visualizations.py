"""Reopen saved Blender scenes and verify positions and missing-frame visibility."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, action="append",
                        choices=[f"20261028_ngt_{i:03}" for i in (2, 3, 4)])
    args = parser.parse_args(sys.argv[sys.argv.index("--")+1:])
    result = {"pid": os.getpid(), "started_utc": datetime.now(UTC).isoformat(),
              "method": "Read saved .blend, inspect all 5Hz frames and half-frame sample hold", "experiments": []}
    for experiment in args.experiment:
        out = ROOT / f"results/{experiment}/position_visualization"
        data = json.loads((out / "scene_data.json").read_text(encoding="utf-8"))
        bpy.ops.wm.open_mainfile(filepath=str(out / "parent_child.blend"))
        scene = bpy.context.scene
        parent = bpy.data.objects["Parent_PROXY_not_actual_dimensions"]
        child = bpy.data.objects["Child_PROXY_not_actual_dimensions"]
        link = bpy.data.objects["Observed_range_ray_line"]
        maximum, visible, missing = 0., 0, 0
        for i, (p, c) in enumerate(zip(data["parent"], data["child"], strict=True)):
            scene.frame_set(i+1, subframe=.5)
            for obj, xyz in [(parent, p), (child, c)]:
                valid = all(x is not None for x in xyz)
                assert obj.hide_render == obj.hide_viewport == (not valid)
                if valid:
                    error = (obj.location-Vector(xyz)).length
                    maximum = max(maximum, error)
                    if error >= 1e-5:
                        action = obj.animation_data.action
                        curves = []
                        for layer in action.layers:
                            for strip in layer.strips:
                                bag = strip.channelbag(obj.animation_data.action_slot)
                                if bag:
                                    curves.extend([(f.data_path, list({k.interpolation for k in f.keyframe_points}))
                                                   for f in bag.fcurves])
                        raise AssertionError({"frame": i+1, "object": obj.name, "error": error,
                                              "expected": xyz, "actual": list(obj.location), "curves": curves})
            if all(x is not None for x in p+c):
                visible += 1
                assert not link.hide_render
                assert abs(link.scale.z-(Vector(c)-Vector(p)).length) < 1e-5
            else:
                missing += 1
                assert link.hide_render
        result["experiments"].append({"experiment": experiment, "frames": len(data["times"]),
            "valid_pair_frames": visible, "missing_pair_frames": missing,
            "max_position_error_m": maximum, "half_frame_sample_hold": "passed"})
    if "20261028_ngt_004" in args.experiment:
        out = ROOT / "results/20261028_ngt_004/map_visualization"
        data = json.loads((out / "map_data.json").read_text(encoding="utf-8"))
        bpy.ops.wm.open_mainfile(filepath=str(out / "wall_map.blend"))
        scene = bpy.data.scenes["All_four_wall_fragments"]
        models = [m for m in data["maps"] if m["model"] != "combined"
                  and m["mode"] == "aquabeacon_prior" and m["valid_structure"]]
        clouds = [o for o in scene.objects if o.name.startswith("Wall_points_")]
        assert len(clouds) == 4 and len(bpy.data.scenes) == 5
        assert sum(o["point_count"] for o in clouds) == sum(len(m["points"]) for m in models)
        result["wall_map"] = {"scenes": 5, "fragments": 4,
                              "points": sum(o["point_count"] for o in clouds)}
    result.update(status="passed", ended_utc=datetime.now(UTC).isoformat())
    out = ROOT / "results/20261028_ngt_000/position_visualization/blender_verification.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
