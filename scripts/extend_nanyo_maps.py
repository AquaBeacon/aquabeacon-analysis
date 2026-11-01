"""Extend the saved Nanyo map while fixing existing camera frames."""

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
import pycolmap as pc

from aquabeacon_analysis.ngt_binary import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261031_nanyo_001"])
    eid = parser.parse_args().experiment
    work = Path(f"data/interim/{eid}/position_mapping")
    source = work / "models"
    original = pc.Reconstruction(source)
    options = pc.IncrementalPipelineOptions()
    options.num_threads = 2
    options.random_seed = 0
    options.fix_existing_frames = True
    options.ba_refine_focal_length = False
    options.ba_refine_principal_point = False
    options.ba_refine_extra_params = False
    options.mapper.abs_pose_refine_focal_length = False
    options.mapper.abs_pose_refine_extra_params = False
    options.ba_global_max_num_iterations = 10
    options.ba_global_max_refinements = 1
    options.ba_local_max_num_iterations = 10
    options.ba_global_frames_ratio = 10
    options.ba_global_points_ratio = 10
    options.multiple_models = False
    options.max_runtime_seconds = 120
    output = work / "extended_models"
    output.mkdir(exist_ok=True)
    models = pc.incremental_mapping(
        work / "features.db", work / "images", output, options=options, input_path=source
    )
    summaries = []
    for model in models.values():
        differences = [
            np.linalg.norm(im.projection_center() - model.image(i).projection_center())
            for i, im in original.images.items()
        ]
        assert max(differences) < 1e-7
        summaries.append(
            {
                "registered_images": model.num_reg_images(),
                "points": model.num_points3D(),
                "existing_camera_center_change_max": max(differences),
                "mean_reprojection_error_px": model.compute_mean_reprojection_error(),
                "names": sorted(im.name for im in model.images.values()),
            }
        )
    manifest = {
        "experiment_id": eid,
        "method": "Continuation with existing frames fixed; 120 s budget",
        "source": str(source),
        "output": str(output),
        "models": summaries,
        "code_sha256": sha256(Path(__file__)),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "inputs_sha256": {str(p): sha256(p) for p in [*source.glob("*.bin"), work / "features.db"]},
    }
    Path(f"results/{eid}/position_mapping/extension_manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )
    print(
        "EXTENSION", [{k: v for k, v in m.items() if k != "names"} for m in summaries], flush=True
    )


if __name__ == "__main__":
    main()
