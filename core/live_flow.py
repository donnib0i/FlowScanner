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


from datetime import date, datetime

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

_SORTS = ["score", "rvol", "notional", "oi_vs_avg"]


def gather_frame(top: int = 30, min_score: int = 35,
                 exclude_etfs: bool = True) -> dict:
    """Real data assembler: universe -> scan -> rollups. Network-bound."""
    import time as _time
    from data.unusual_flow import scan_unusual_flow
    from data.sources import available_sources
    from data.baseline import BaselineStore

    store = BaselineStore()
    t0 = _time.time()
    signals = scan_unusual_flow(min_score=min_score, max_results=top * 5,
                                exclude_etfs=exclude_etfs)
    secs = round(_time.time() - t0, 1)

    agg = aggregate_by_ticker(signals)
    today = date.today().isoformat()
    leaders = []
    for tkr, a in sorted(agg.items(),
                         key=lambda kv: kv[1]["opt_vol"], reverse=True)[:top]:
        store.record_ticker(today, tkr, a["opt_vol"], a["opt_oi"], 0)
        leaders.append({
            "ticker": tkr, "sector": a["sector"], "price": 0.0, "pct": 0.0,
            "spark": "", "rvol": 0.0, "opt_vol": a["opt_vol"], "opt_oi": a["opt_oi"],
            "optvol_rvol": store.ticker_optvol_rvol(tkr, a["opt_vol"]) or 0.0,
            "call_notional": a["call_notional"], "put_notional": a["put_notional"],
        })
    for s in signals:
        store.record_contract(today, s["ticker"], s["type"], s["strike"],
                              s["expiry"], s["open_interest"], s["volume"])
    call, put = net_flow(signals)
    srcs = available_sources()
    return {
        "leaders": leaders,
        "contracts": [dict(s) for s in signals[:top]],
        "net_call": call, "net_put": put,
        "source": (srcs[0] if srcs else "yfinance").upper(),
        "live": bool(srcs and srcs[0] not in ("yfinance", "yahoo")),
        "universe_size": len(agg), "scan_secs": secs,
    }


class FlowDeckApp(App):
    CSS = """
    Screen { background: #0a0a0f; }
    #status { height: 1; color: #00ff88; background: #11131a; }
    DataTable { background: #0a0a0f; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("p", "toggle_pause", "Pause"),
        ("s", "cycle_sort", "Sort"),
        ("r", "refresh_now", "Refresh"),
    ]

    def __init__(self, data_fn=None, interval: int = 45,
                 top: int = 30, min_score: int = 35):
        super().__init__()
        self.data_fn = data_fn or (lambda: gather_frame(top, min_score))
        self.interval = interval
        self.sort_key = "score"
        self.paused = False
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("FLOWDECK ▸ starting…", id="status")
        with Vertical():
            yield Static("VOLUME LEADERS (single stocks)", classes="hdr")
            yield DataTable(id="leaders")
            yield Static("UNUSUAL CONTRACTS (driving it)", classes="hdr")
            yield DataTable(id="contracts")

    def on_mount(self) -> None:
        lt = self.query_one("#leaders", DataTable)
        lt.add_columns("TICKER", "SECTOR", "PRICE", "%CHG", "VOL",
                       "RVOL", "OPT VOL", "OPT OI", "OPTvAVG", "FLOW")
        ct = self.query_one("#contracts", DataTable)
        ct.add_columns("SIGNAL", "TICKER", "STRIKE", "EXP", "DTE", "SIDE",
                       "VOL", "OI", "V/OI", "OIvAVG", "NOTIONAL", "SCR")
        self.refresh_now_worker()
        if self.interval and self.interval > 0:
            self._timer = self.set_interval(self.interval, self.refresh_now_worker)

    # ── data refresh runs off the UI thread ──
    def refresh_now_worker(self) -> None:
        if self.paused:
            return
        self.run_worker(self._do_refresh, thread=True, exclusive=True)

    def _do_refresh(self) -> None:
        try:
            frame = self.data_fn()
        except Exception as e:  # never let the loop die
            self.call_from_thread(self._set_status, f"error: {e}")
            return
        self.call_from_thread(self._render, frame)

    def _ticker_oi_str(self, v) -> str:
        return f"{v:.1f}x" if isinstance(v, (int, float)) and v else "NEW"

    def _render(self, frame: dict) -> None:
        lt = self.query_one("#leaders", DataTable)
        lt.clear()
        leaders = frame["leaders"]
        if self.sort_key == "rvol":
            leaders = sorted(leaders, key=lambda r: r["optvol_rvol"], reverse=True)
        for r in leaders:
            flow = r["call_notional"] - r["put_notional"]
            arrow = "🟢" if flow >= 0 else "🔴"
            lt.add_row(
                r["ticker"], r["sector"], f'{r["price"]:.2f}', f'{r["pct"]:+.1f}%',
                r["spark"], f'{r["rvol"]:.1f}x', fmt_compact(r["opt_vol"]),
                fmt_compact(r["opt_oi"]), self._ticker_oi_str(r["optvol_rvol"]),
                f'{arrow} {fmt_compact(flow)}',
            )
        ct = self.query_one("#contracts", DataTable)
        ct.clear()
        contracts = frame["contracts"]
        keymap = {"score": "score", "notional": "notional",
                  "oi_vs_avg": "ticker_oi_vs_avg", "rvol": "score"}
        k = keymap.get(self.sort_key, "score")
        contracts = sorted(contracts, key=lambda c: (c.get(k) or 0), reverse=True)
        for c in contracts:
            t = "C" if c["type"] == "call" else "P"
            ct.add_row(
                c["label"], c["ticker"], f'{t}{int(c["strike"])}', c["expiry"],
                f'{c["dte"]}d', c.get("trade_side", ""), fmt_compact(c["volume"]),
                fmt_compact(c["open_interest"]), f'{c["vol_oi"]:.1f}x',
                self._ticker_oi_str(c.get("ticker_oi_vs_avg")),
                f'${fmt_compact(c["notional"])}', str(c["score"]),
            )
        net = frame["net_call"] - frame["net_put"]
        tag = "LIVE" if frame["live"] else "DELAYED"
        self._set_status(
            f'FLOWDECK ▸ {datetime.now():%H:%M:%S}  NET {fmt_compact(net)}  '
            f'{frame["source"]} {tag} · {frame["universe_size"]} names · '
            f'scan {frame["scan_secs"]}s · sort:{self.sort_key}'
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    # ── actions ──
    def action_cycle_sort(self) -> None:
        i = (_SORTS.index(self.sort_key) + 1) % len(_SORTS)
        self.sort_key = _SORTS[i]
        self.refresh_now_worker()

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused

    def action_refresh_now(self) -> None:
        self.refresh_now_worker()


def run(interval: int = 45, top: int = 30, min_score: int = 35) -> None:
    FlowDeckApp(interval=interval, top=top, min_score=min_score).run()
