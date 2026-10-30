"""Auditable quality-priority selection; never average correlated GNSS solutions."""
import numpy as np

STATUS = {-1: 'Missing', 0: 'Invalid/unsupported', 1: 'Single', 2: 'DGPS',
          3: 'Float', 4: 'Fixed'}


def quality_rank(row, source):
    if row is None:
        return -1
    if not all(np.isfinite(float(row[k])) for k in
               ['utc_s', 'east_m', 'north_m', 'up_m']):
        return 0
    if source == 'RTK':
        if (str(row['fix_valid']) != 'True' or str(row['utc_valid']) != 'True'
                or int(row['fix_type']) not in (3, 4)):
            return 0
        carrier = int(row['carrier_solution'])
        return {2: 4, 1: 3, 0: 2 if str(row['differential']) == 'True' else 1}.get(carrier, 0)
    return {1: 4, 2: 3, 4: 2, 5: 1}.get(int(row['quality']), 0)


def paired_epochs(rtk, ppk, tolerance_s=.02):
    """One-to-one sorted epoch union; retain unmatched rows, never interpolate."""
    for rows in (rtk, ppk):
        times = np.array([float(r['utc_s']) for r in rows])
        if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
            raise ValueError('Epochs must have finite, unique, increasing UTC')
        if len(times) > 1 and np.min(np.diff(times)) <= 2*tolerance_s:
            raise ValueError('Ambiguous matching: cadence too close to tolerance')
    i = j = 0
    while i < len(rtk) or j < len(ppk):
        a = rtk[i] if i < len(rtk) else None
        b = ppk[j] if j < len(ppk) else None
        if a is not None and b is not None and abs(float(a['utc_s'])-float(b['utc_s'])) <= tolerance_s:
            yield a, b
            i += 1
            j += 1
        elif b is None or (a is not None and float(a['utc_s']) < float(b['utc_s'])):
            yield a, None
            i += 1
        else:
            yield None, b
            j += 1


def select_positions(rtk, ppk, tolerance_s=.02):
    """Fixed > Float > DGPS > Single; equal valid quality prefers final PPK."""
    result = []
    previous = None
    for a, b in paired_epochs(rtk, ppk, tolerance_s):
        ranks = [quality_rank(a, 'RTK'), quality_rank(b, 'PPK')]
        source = ('PPK' if ranks[1] >= ranks[0] else 'RTK') if max(ranks) > 0 else 'None'
        selected = {'RTK': a, 'PPK': b}.get(source)
        anchor = a if a is not None else b
        row = {'utc_s': float(anchor['utc_s']), 'parent_video_s': float(anchor['parent_video_s']),
               'rtk_status': STATUS[ranks[0]], 'ppk_status': STATUS[ranks[1]],
               'rtk_rank': ranks[0], 'ppk_rank': ranks[1], 'selected_source': source,
               'selected_status': STATUS[max(ranks)] if selected else 'Missing/invalid',
               'selected_utc_s': float(selected['utc_s']) if selected else np.nan,
               'rtk_utc_s': float(a['utc_s']) if a else np.nan,
               'ppk_utc_s': float(b['utc_s']) if b else np.nan,
               'rtk_carrier_code': a['carrier_solution'] if a else '',
               'ppk_quality_code': b['quality'] if b else '',
               'match_delta_s': float(b['utc_s'])-float(a['utc_s']) if a and b else np.nan,
               'reason': ('no_valid_solution' if not selected else
                          'equal_quality_prefer_final_ppk' if ranks[0] == ranks[1] else
                          'higher_quality_or_only_valid'),
               'source_switch': bool(previous and source != 'None'
                                     and previous['selected_source'] not in ('None', source))}
        for key in ['east_m', 'north_m', 'up_m', 'latitude_deg', 'longitude_deg',
                    'height_ellipsoid_m']:
            row[key] = float(selected[key]) if selected else np.nan
        row['rtk_ppk_horizontal_difference_m'] = (
            np.hypot(float(a['east_m'])-float(b['east_m']),
                     float(a['north_m'])-float(b['north_m'])) if min(ranks) > 0 else np.nan)
        row['switch_step_horizontal_m'] = (
            np.hypot(row['east_m']-previous['east_m'], row['north_m']-previous['north_m'])
            if row['source_switch'] else np.nan)
        result.append(row)
        previous = row
    return result
