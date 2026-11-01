"""Verify texture geometry, navigation provenance and the actual local browser replay."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import pycolmap as pc
from analyze_nanyo import read
from playwright.sync_api import sync_playwright

from aquabeacon_analysis.camera_calibration import load_camera_intrinsics
from aquabeacon_analysis.ngt_binary import sha256
from aquabeacon_analysis.ngt_textured_map import camera_projection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, choices=["20261031_nanyo_000"])
    parser.add_argument(
        "--browser-executable",
        default="C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    )
    eid = parser.parse_args().experiment
    browser_executable = parser.parse_args().browser_executable
    out = Path(f"results/{eid}/navigation_replay")
    checks = []
    media_checks = []
    media = json.loads((out / "media_manifest.json").read_text())
    for item in media["videos"]:
        preview = cv2.VideoCapture(str(Path(item["target"])))
        midpoint = (item["end_s"] - item["start_s"]) / 2
        preview.set(cv2.CAP_PROP_POS_MSEC, midpoint * 1000)
        ok, frame = preview.read()
        assert ok
        preview_pts = preview.get(cv2.CAP_PROP_POS_MSEC) / 1000
        preview.release()
        requested = item["source_start_s"] + preview_pts / item["time_scale"]
        source = cv2.VideoCapture(item["source"])
        source.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        candidates = []
        for delta in np.arange(-0.1, 0.101, 1 / 30):
            source.set(cv2.CAP_PROP_POS_MSEC, (requested + delta) * 1000)
            ok, raw = source.read()
            if not ok:
                continue
            actual = source.get(cv2.CAP_PROP_POS_MSEC) / 1000
            resized = cv2.resize(raw, (640, 360), interpolation=cv2.INTER_CUBIC)
            error = float(np.mean(abs(resized.astype(float) - frame.astype(float))))
            candidates.append((error, actual - requested))
        source.release()
        best = min(candidates)
        assert best[0] < 15 and abs(best[1]) < 0.11, (item["path"], best)
        media_checks.append(
            {
                "video": item["path"],
                "preview_time_s": preview_pts,
                "matched_native_clock_difference_s": best[1],
                "mean_pixel_difference_0_255": best[0],
            }
        )
    k, _, _ = load_camera_intrinsics(Path.cwd(), "C_MAIN_CAM", (960, 540), "proxy")
    for suffix in ["001", "002"]:
        segment = f"20261031_nanyo_{suffix}"
        scene_path = Path(f"results/{segment}/navigation_mapping/scene.json")
        scene = json.loads(scene_path.read_text(encoding="utf-8"))
        points = np.array(scene["points"])
        maximum = 0
        for s in scene["surfaces"]:
            vertices = np.array(s["vertices"])
            np.testing.assert_allclose(vertices, points[s["point_indices"]], atol=1e-10)
            px, depth = camera_projection(vertices, s["camera"], s["quaternion_xyzw"], k)
            uv = np.array(s["uv"])
            expected = np.column_stack([px[:, 0] / 960, 1 - px[:, 1] / 540])
            maximum = max(maximum, float(np.max(abs(expected - uv))))
            assert np.all(depth > 0) and maximum < 1e-8 and np.all((uv >= 0) & (uv <= 1))
        navigation = read(Path(f"results/{segment}/navigation_fusion/child_navigation.csv"))
        assert all(r["sfm_position_used"] == "False" for r in navigation)
        assert all(
            float(r["anchor_age_s"]) <= 3.00001
            for r in navigation
            if r["position_available"] == "True"
        )
        model = pc.Reconstruction(f"data/processed/{segment}/navigation_mapping/model")
        assert model.num_points3D() == len(points)
        checks.append(
            {
                "experiment_id": segment,
                "texture_uv_max_error": maximum,
                "photo_patches": len(scene["surfaces"]),
                "points": len(points),
                "sfm_navigation_used": False,
            }
        )
    errors = []
    states = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=browser_executable,
            headless=True,
            args=["--enable-unsafe-swiftshader", "--allow-file-access-from-files"],
        )
        page = browser.new_page(viewport={"width": 1500, "height": 1050}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto((out / "viewer.html").resolve().as_uri(), wait_until="load", timeout=60000)
        page.wait_for_function(
            "window.NANYO_REPLAY_STATE && NANYO_REPLAY_STATE.texturesReady === NANYO_REPLAY_STATE.surfaces",
            timeout=60000,
        )
        page.wait_for_function(
            "['child','parent'].every(r=>document.getElementById(r+'Video').readyState>=2)",
            timeout=30000,
        )
        page.screenshot(path=str(out / "preview_001.png"), full_page=True)
        for index, time in [(0, 650), (0, 0), (1, 100), (1, 280)]:
            page.evaluate(
                "([index,time])=>{NANYO_REPLAY.chooseSegment(index);NANYO_REPLAY.setTime(time,true);}",
                [index, time],
            )
            page.wait_for_function(
                "NANYO_REPLAY_STATE.texturesReady === NANYO_REPLAY_STATE.surfaces", timeout=30000
            )
            page.wait_for_function(
                "['child','parent'].every(r=>{let v=document.getElementById(r+'Video');return v.readyState>=2 && !v.seeking;})",
                timeout=30000,
            )
            state = page.evaluate("""()=>({state:NANYO_REPLAY_STATE,videos:['child','parent'].map(role=>{
                let v=document.getElementById(role+'Video');let d=NANYO_REPLAY.data;let s=d.segments[NANYO_REPLAY_STATE.selected];
                let part=d.media.find(m=>m.experiment_id===s.id&&m.role===role&&NANYO_REPLAY_STATE.time>=m.start_s&&NANYO_REPLAY_STATE.time<m.end_s);
                return {role,source:v.dataset.source,current:v.currentTime,expected:NANYO_REPLAY_STATE.time-part.start_s,width:v.videoWidth,height:v.videoHeight};})})""")
            for video in state["videos"]:
                assert video["width"] == 640 and video["height"] == 360
                assert abs(video["current"] - video["expected"]) < 0.15
            if index == 0 and time == 0:
                assert not state["state"]["childAvailable"]
            states.append(state)
        page.screenshot(path=str(out / "preview_002.png"), full_page=True)
        page.locator("#showVideos").uncheck()
        assert not page.locator("#videos").is_visible()
        page.locator("#showVideos").check()
        page.locator("#mesh").uncheck()
        page.locator("#points").check()
        page.locator("#overall").click()
        page.locator("#sourceView").click()
        before = page.evaluate("NANYO_REPLAY_STATE.time")
        page.locator("#play").click()
        page.wait_for_timeout(900)
        page.locator("#play").click()
        assert page.evaluate("NANYO_REPLAY_STATE.time") > before + 0.3
        assert not errors, errors
        browser.close()
    report = {
        "status": "passed",
        "geometry": checks,
        "native_preview_frame_checks": media_checks,
        "browser_states": states,
        "page_errors": errors,
        "checked_controls": [
            "segment switch",
            "time seek",
            "parent split-video boundary",
            "missing child position",
            "play/pause",
            "video visibility",
            "mesh/point toggle",
            "view resets",
        ],
        "viewer_sha256": sha256(out / "viewer.html"),
        "code_sha256": sha256(Path(__file__)),
    }
    (out / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Texture, provenance, paired-video seek and replay controls passed.", flush=True)


if __name__ == "__main__":
    main()
