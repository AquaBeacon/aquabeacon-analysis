import numpy as np

from aquabeacon_analysis.ngt_optical import detect_candidate


def test_missing_and_edge_and_candidate():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert detect_candidate(frame)["status"] == "missing"
    frame[30:40, 30:40] = 255
    result = detect_candidate(frame)
    assert result["status"] == "candidate"
    assert result["x_px"] == 34.5
    frame[:] = 0
    frame[0:10, 30:40] = 255
    assert detect_candidate(frame)["status"] == "excluded"
