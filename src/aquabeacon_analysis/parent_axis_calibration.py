"""Parent/Spresense relative clock reconstruction without promoting phone time to GNSS."""
import numpy as np


def reference_clock_fit(health, references, session):
    """Invert shared phone anchors; fit receipt-clock offset with explicit latency caveat."""
    anchors = sorted({(r["utc_anchor_mono_us"], r["utc_anchor_epoch_us"], r["utc_source"])
                      for r in health})
    pairs = []
    for r in references:
        if r["parent_time_source"] != 2 or r["parent_session"] != session:
            continue
        epoch = r["utc_s"] * 1000000 + r["nav_usec"]
        candidates = []
        for i, (mono, utc, source) in enumerate(anchors):
            stop = anchors[i+1][0] if i+1 < len(anchors) else health[-1]["mono_us"]
            candidate = mono + epoch - utc
            if source == "phone_clock" and mono <= candidate < stop:
                candidates.append(candidate)
        if len(candidates) == 1:
            pairs.append([r["parent_reference_mono_us"]/1e6, candidates[0]/1e6])
    points = np.asarray(pairs)
    if len(points) < 10 or np.ptp(points[:, 0]) < 30:
        raise ValueError("Insufficient unique shared reference anchors")
    origin = float(points[:, 0].mean())
    slope, intercept = np.polyfit(points[:, 0]-origin, points[:, 1]-points[:, 0], 1)
    residual = points[:, 1]-points[:, 0] - (intercept+slope*(points[:, 0]-origin))
    if abs(residual).max() > .01 or abs(slope) > .001:
        raise ValueError("Reference clock discontinuity")
    return {"origin_s": origin, "offset_at_origin_s": float(intercept),
            "offset_slope": float(slope), "pairs": points.tolist(),
            "residual_max_s": float(abs(residual).max()),
            "equation": "parent_mono = spresense_mono + offset_at_origin + offset_slope*(spresense_mono-origin)",
            "absolute_utc_validated": False,
            "limitation": "Shared phone timestamps cancel as reference labels; packet receipt latency is unmeasured. Early RTC interval uses same-boot affine extrapolation."}


def parent_time(seconds, fit):
    t = np.asarray(seconds)
    return t + fit["offset_at_origin_s"] + fit["offset_slope"]*(t-fit["origin_s"])
