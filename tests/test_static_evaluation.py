import numpy as np
import pytest

from aquabeacon_analysis.static_evaluation import static_metrics


def test_population_rmse_and_translation_invariance():
    points = np.array([[1, 2, 3], [-1, -2, -3]])
    result = static_metrics(points)
    assert result["rmse_3d_m"] == pytest.approx(np.sqrt(14))
    assert result["horizontal_rmse_m"] == pytest.approx(np.sqrt(5))
    assert static_metrics(points + 100)["axis_rmse_m"] == result["axis_rmse_m"]
    assert static_metrics(np.ones((5, 3)))["rmse_3d_m"] == 0


def test_missing_not_silently_removed():
    with pytest.raises(ValueError):
        static_metrics([[1, np.nan, 3]])
    with pytest.raises(ValueError):
        static_metrics([])
