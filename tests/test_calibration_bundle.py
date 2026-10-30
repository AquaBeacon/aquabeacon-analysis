"""Portable evidence must remain usable without the original raw/results directories."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_calibration_bundle", ROOT / "scripts/verify_calibration_bundle.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def test_published_evidence_reproduces_calibrations():
    result = verifier.verify(ROOT, refit=True)
    assert set(result["axis_refits"]) == {"parent", "child"}
    assert {v["views"] for v in result["camera_refits"].values()} == {34, 35}


@pytest.mark.parametrize("content", [None, b"modified"])
def test_missing_or_modified_evidence_is_rejected(tmp_path, content):
    bundle = tmp_path / "calibration/reference_data"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text(json.dumps({"files": [
        {"path": "calibration/reference_data/evidence.csv", "sha256": "0" * 64}]}))
    if content is not None:
        (bundle / "evidence.csv").write_bytes(content)
    with pytest.raises(ValueError, match="Missing calibration|checksum mismatch"):
        verifier.verify(tmp_path)
