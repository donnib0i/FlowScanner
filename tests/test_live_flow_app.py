import pytest
from textual.widgets import DataTable
from core.live_flow import FlowDeckApp

FAKE_FRAME = {
    "leaders": [
        {"ticker": "NVDA", "sector": "Tech", "price": 142.3, "pct": 2.1,
         "spark": "▁▂▄▇█", "rvol": 4.8, "opt_vol": 214000, "opt_oi": 1820000,
         "optvol_rvol": 3.1, "call_notional": 3_200_000, "put_notional": 0},
    ],
    "contracts": [
        {"label": "🔴 EXTREME", "ticker": "NVDA", "type": "call", "strike": 145,
         "expiry": "2026-06-13", "dte": 2, "trade_side": "ask",
         "volume": 18000, "open_interest": 3000, "vol_oi": 6.1,
         "ticker_oi_vs_avg": 6.1, "notional": 3_200_000, "score": 88},
    ],
    "net_call": 3_200_000, "net_put": 0,
    "source": "TEST", "live": False, "universe_size": 1, "scan_secs": 0.0,
}


async def test_app_populates_both_tables():
    app = FlowDeckApp(data_fn=lambda: FAKE_FRAME, interval=0)
    async with app.run_test() as pilot:
        await pilot.pause()
        leaders = app.query_one("#leaders", DataTable)
        contracts = app.query_one("#contracts", DataTable)
        assert leaders.row_count == 1
        assert contracts.row_count == 1


async def test_sort_hotkey_changes_sort_key():
    app = FlowDeckApp(data_fn=lambda: FAKE_FRAME, interval=0)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = app.sort_key
        await pilot.press("s")
        assert app.sort_key != before
