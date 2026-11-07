"""Pure helpers for same-image SfM ablations and temporal match connectivity."""
from __future__ import annotations

import numpy as np


def compare_priors(old, new):
    """Compare availability separately from finite coordinates; never fill gaps."""
    old, new = np.asarray(old, float), np.asarray(new, float)
    if old.shape != new.shape or old.ndim != 2 or old.shape[1] != 3:
        raise ValueError("Prior arrays must have the same N x 3 shape")
    a, b = np.isfinite(old).all(1), np.isfinite(new).all(1)
    common = a & b
    distances = np.linalg.norm(old[common] - new[common], axis=1)
    return {"old_available": int(a.sum()), "new_available": int(b.sum()),
            "availability_changes": int(np.count_nonzero(a != b)),
            "common": int(common.sum()), "changed_coordinates": int(np.count_nonzero(distances != 0)),
            "maximum_difference_m": float(distances.max()) if distances.size else None,
            "identical": bool(np.array_equal(a, b) and np.array_equal(old[common], new[common]))}


def temporal_connectivity(count, edges):
    """Frame graph components and links across each time cut, including skipped frames.

    Edges are verified image pairs, not online SLAM tracks. Isolated images remain
    separate components. A zero cut cannot be bridged by any supplied pair.
    """
    parent = list(range(count))
    crossing = np.zeros(max(0, count-1), dtype=int)
    degree = np.zeros(count, dtype=int)

    def find(x):
        while x != parent[x]:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    unique = {tuple(sorted(map(int, edge))) for edge in edges}
    for a, b in unique:
        if not 0 <= a < b < count:
            raise ValueError("Edges must connect distinct valid frame indices")
        parent[find(b)] = find(a)
        degree[a] += 1; degree[b] += 1
        crossing[a:b] += 1
    roots = [find(i) for i in range(count)]
    keys = sorted(set(roots), key=roots.index)
    labels = np.array([keys.index(x) for x in roots])
    return {"labels": labels, "degree": degree, "crossing": crossing,
            "component_sizes": sorted([roots.count(k) for k in keys], reverse=True)}


def zero_cut_intervals(crossing, times):
    """Return runs of consecutive unbridged cuts using measured frame timestamps."""
    crossing, times = np.asarray(crossing), np.asarray(times, float)
    if len(times) != len(crossing)+1 or np.any(np.diff(times) <= 0):
        raise ValueError("Strictly ordered frame times must bracket every cut")
    result = []
    start = None
    for i, value in enumerate(crossing):
        if value == 0 and start is None:
            start = i
        if start is not None and (value != 0 or i == len(crossing)-1):
            stop = i if value != 0 else i+1
            result.append({"first_cut": start, "last_cut": stop-1,
                           "start_s": float(times[start]), "end_s": float(times[stop]),
                           "duration_s": float(times[stop]-times[start])})
            start = None
    return result
