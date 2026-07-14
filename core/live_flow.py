"""
live_flow.py — FlowDeck: live single-stock volume + unusual options TUI.

This module has two halves:
  * pure helpers (sparkline, rollups, formatting) — unit tested
  * the Textual App (added in Task 7) — smoke tested via Pilot
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list) -> str:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return _BARS[0] * len(vals)
    return "".join(_BARS[int((v - lo) / (hi - lo) * (len(_BARS) - 1))] for v in vals)


def aggregate_by_ticker(signals: List[Dict]) -> Dict[str, Dict]:
    agg: Dict[str, Dict] = {}
    for s in signals:
        t = s["ticker"]
        a = agg.setdefault(t, {
            "sector": s.get("sector", "?"),
            "opt_vol": 0, "opt_oi": 0,
            "call_notional": 0.0, "put_notional": 0.0,
        })
        a["opt_vol"] += int(s.get("volume", 0))
        a["opt_oi"] += int(s.get("open_interest", 0))
        if s.get("type") == "call":
            a["call_notional"] += s.get("notional", 0)
        else:
            a["put_notional"] += s.get("notional", 0)
    return agg


def net_flow(signals: List[Dict]) -> Tuple[float, float]:
    call = sum(s.get("notional", 0) for s in signals if s.get("type") == "call")
    put = sum(s.get("notional", 0) for s in signals if s.get("type") == "put")
    return call, put


def fmt_compact(n: float) -> str:
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"
