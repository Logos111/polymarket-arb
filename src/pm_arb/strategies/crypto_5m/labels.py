"""Backtest-only label construction (FUTURE INFO; b09 C).

GUARDRAIL: this module must never be imported by orchestrator.py /
decisions.py / context.py. tests/test_no_lookahead_leak.py enforces
this statically. Any future-window computation belongs here.

"""
from __future__ import annotations


def _asof(ts, vals, t, max_age=5.0):
    """Last value at or before t, None if missing or stale."""
    lo, hi = 0, len(ts)
    while lo < hi:
        mid = (lo + hi) // 2
        if ts[mid] <= t:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0 or t - ts[lo - 1] > max_age:
        return None
    return vals[lo - 1]


def compute_window_labels(rows, ticks, mkt):
    """Attach study labels in place (opt-plan section 20, backtest only)."""
    ts = [tk["t"] for tk in ticks]
    mids = {}
    bids = {}
    for side, bcol, acol in (("Up", "bu", "au"), ("Down", "bd", "ad")):
        mids[side] = []
        bids[side] = []
        for tk in ticks:
            av, bv = tk[acol], tk[bcol]
            ok = av and av > 0 and bv and bv > 0
            bids[side].append(bv if ok else None)
            mids[side].append((av + bv) / 2 if ok else None)
    for r in rows:
        side = r["cand"]
        t0 = r["t"]
        m0 = _asof(ts, mids[side], t0)
        if m0 is not None and m0 > 0:
            for n in (30, 60, 120):
                m1 = _asof(ts, mids[side], t0 + n)
                if m1 is not None:
                    r["future_return_" + str(n)] = (m1 - m0) / m0
        a0 = r.get("cand_ask")
        r["final_outcome"] = mkt.outcome
        if a0 is None:
            continue
        hi_b = None
        lo_b = None
        for j, t1 in enumerate(ts):
            if t0 < t1 <= t0 + 60:
                b1 = bids[side][j]
                if b1 is not None:
                    hi_b = b1 if hi_b is None else max(hi_b, b1)
                    lo_b = b1 if lo_b is None else min(lo_b, b1)
        if hi_b is not None and lo_b is not None:
            r["mfe_60"] = hi_b - a0
            r["mae_60"] = a0 - lo_b
