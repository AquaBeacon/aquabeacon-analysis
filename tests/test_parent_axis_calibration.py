import numpy as np
import pytest

from aquabeacon_analysis.parent_axis_calibration import parent_time, reference_clock_fit


def test_shared_phone_clock_and_session_rejection():
    epoch = 1793280000000000
    health = [{"utc_anchor_mono_us": 0, "utc_anchor_epoch_us": epoch,
               "utc_source": "phone_clock", "mono_us": 200000000}]
    refs = [{"parent_time_source": 2, "parent_session": 10,
             "utc_s": (epoch+(i+2)*1000000)//1000000, "nav_usec": 0,
             "parent_reference_mono_us": i*1000000} for i in range(10, 110, 5)]
    fit = reference_clock_fit(health, refs, 10)
    np.testing.assert_allclose(parent_time([5, 15], fit), [7, 17], atol=1e-8)
    assert not fit["absolute_utc_validated"]
    with pytest.raises(ValueError, match="Insufficient"):
        reference_clock_fit(health, refs, 11)
