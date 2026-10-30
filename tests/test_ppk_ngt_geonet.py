"""Check official RINEX UTC handling and PPK epoch/frame conversion."""
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location(
    'ppk_ngt_geonet', Path(__file__).parents[1]/'scripts/ppk_ngt_geonet.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_rinex_observation_epochs_are_gps_time(tmp_path):
    path = tmp_path/'base.obs'
    path.write_text(' -3723694.9786 3701367.9415 3609416.0055'.ljust(60)+'APPROX POSITION XYZ\n'
                    '                                                            END OF HEADER\n'
                    '> 2026 10 28 05 00 00.0000000 0 1\n'
                    '> 2026 10 28 05 00 30.0000000 0 1\n')
    info = module.observations(path)
    assert info['first_utc_s'] == datetime(2026, 10, 28, 4, 59, 42, tzinfo=UTC).timestamp()
    assert info['interval_s'] == 30
    assert info['epochs'] == 2


def test_ppk_status_age_ratio_frame_and_video_time_retained(tmp_path):
    path = tmp_path/'output.pos'
    path.write_text('% header\n2026/10/28 05:00:00.000 34 135 10 1 8 .1 .2 .3 0 0 0 -15 4.5\n')
    utc = datetime(2026, 10, 28, 5, tzinfo=UTC).timestamp()
    rows = module.read_positions(path, [34, 135, 10],
                                 {'utc_at_video_zero_s': utc-100, 'time_scale': 2})
    assert rows[0]['quality'] == 1
    assert rows[0]['ratio'] == 4.5
    assert rows[0]['age_s'] == -15
    assert rows[0]['parent_video_s'] == 50
    np.testing.assert_allclose([rows[0][k] for k in ['east_m', 'north_m', 'up_m']], 0,
                               atol=1e-8)
    path.write_text('% no solution\n')
    with pytest.raises(ValueError, match='no positions'):
        module.read_positions(path, [34, 135, 10], {})
