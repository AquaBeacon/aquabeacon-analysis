"""Explicit event grouping and valid clock-anchor mapping for analysis subsets."""
import itertools
from bisect import bisect_right

import numpy as np


def group_events(times_s, merge_gap_s=30., padding_s=10.):
    times = np.unique(np.asarray(times_s, dtype=float))
    times = times[np.isfinite(times)]
    if not len(times):
        return []
    cuts = np.r_[0, np.flatnonzero(np.diff(times) > merge_gap_s)+1, len(times)]
    return [{'active_start_utc_s': float(times[a]), 'active_end_utc_s': float(times[b-1]),
             'start_utc_s': float(times[a]-padding_s),
             'end_utc_s': float(times[b-1]+padding_s)}
            for a, b in itertools.pairwise(cuts)]


def valid_anchors(rows):
    result = sorted({(int(r['utc_anchor_mono_us']), int(r['utc_anchor_epoch_us']),
                      int(r['utc_valid_until_us'])) for r in rows
                     if int(r['utc_anchor_epoch_us']) > 10**15
                     and int(r['utc_valid_until_us']) > int(r['utc_anchor_mono_us'])})
    return [r[0] for r in result], result


def map_anchor(mono_us, anchors):
    starts, records = anchors
    i = bisect_right(starts, mono_us)-1
    if i < 0:
        return None
    start, epoch, end = records[i]
    return (epoch+mono_us-start)/1e6 if start <= mono_us < end else None


def intersect_activity(immersions, ranging, minimum_duration_s=10.):
    """Intersect water occupancy with parent ranging, without OR or padding.

    Inputs are half-open intervals. Separate ranging runs remain separate even
    if the child stays underwater; failed ranges still belong to their run.
    """
    result = []
    for water in immersions:
        for run in ranging:
            start = max(water['start_utc_s'], run['start_utc_s'])
            end = min(water['end_utc_s'], run['end_utc_s'])
            if end-start >= minimum_duration_s:
                result.append({'start_utc_s': start, 'end_utc_s': end,
                               'immersion_start_utc_s': water['start_utc_s'],
                               'parent_ranging_start_utc_s': run['start_utc_s']})
    return sorted(result, key=lambda row: row['start_utc_s'])
