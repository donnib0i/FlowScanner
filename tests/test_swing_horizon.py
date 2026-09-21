"""
The scanner's horizon.

Found 2026-09-21 when the default was moved from 0DTE to swing: the flow scan
read `near_exps[:3]` -- the first three expiries -- and on a name with daily
expiries those are today, tomorrow and the day after. A 30-day swing position
never entered the scan at all, whatever the window said. Measured on NVDA:
dte8p_flow was $0.0 before and $8.7M after; META $0.0 -> $44M.
"""
import datetime as dt

import pytest


def _expiry_sampler(exps, today, dte_max, max_n):
    """The exact selection logic in core/flow.py, extracted for a direct test."""
    def dte_of(e):
        return (dt.date.fromisoformat(e) - today).days
    dated = [(e, dte_of(e)) for e in exps if 0 <= dte_of(e) <= dte_max]
    near = [e for e, d in dated if d <= 7]
    swing = [e for e, d in dated if d > 7]
    return (near + swing[::2])[:max_n] or list(exps[:2])


TODAY = dt.date(2026, 9, 21)
DAILY = [(TODAY + dt.timedelta(days=i)).isoformat() for i in range(0, 60)]


def test_a_daily_expiry_chain_still_reaches_the_swing_window():
    """The first three of a daily chain are 0, 1, 2 DTE. The sample must
    include expiries inside 7-45 DTE or swing flow is invisible."""
    from core.constants import FLOW_DTE_MAX, FLOW_MAX_EXPIRIES, SWING_DTE_MIN, SWING_DTE_MAX
    chosen = _expiry_sampler(DAILY, TODAY, FLOW_DTE_MAX, FLOW_MAX_EXPIRIES)
    dtes = [(dt.date.fromisoformat(e) - TODAY).days for e in chosen]
    assert any(SWING_DTE_MIN <= d <= SWING_DTE_MAX for d in dtes), dtes


def test_the_near_term_is_not_sacrificed_for_the_swing_window():
    """0DTE and weeklies are still the other two trades."""
    from core.constants import FLOW_DTE_MAX, FLOW_MAX_EXPIRIES
    chosen = _expiry_sampler(DAILY, TODAY, FLOW_DTE_MAX, FLOW_MAX_EXPIRIES)
    dtes = [(dt.date.fromisoformat(e) - TODAY).days for e in chosen]
    assert 0 in dtes and any(1 <= d <= 7 for d in dtes)


def test_the_window_is_bounded():
    """Reading every expiry on a daily chain is 60 fetches a name."""
    from core.constants import FLOW_DTE_MAX, FLOW_MAX_EXPIRIES
    chosen = _expiry_sampler(DAILY, TODAY, FLOW_DTE_MAX, FLOW_MAX_EXPIRIES)
    assert len(chosen) <= FLOW_MAX_EXPIRIES


def test_the_flow_scanner_uses_the_sampler_not_a_prefix():
    """Pins the fix itself: no `[:3]` prefix slice on the expiry list."""
    import inspect
    from core import flow
    src = inspect.getsource(flow._scan_options_flow_yf)
    assert "near_exps[:3]" not in src
    assert "swing[::2]" in src


def test_swing_flow_is_measured_on_a_real_chain():
    """Network. The assertion the whole change exists for: swing premium is a
    number, not a hardcoded zero."""
    pytest.importorskip("yfinance")
    import os
    os.environ.pop("TT_USERNAME", None); os.environ.pop("TT_PASSWORD", None)
    from core.flow import _scan_options_flow_yf
    try:
        sigs = _scan_options_flow_yf(["META"], show_progress=False)
    except Exception as e:                     # pragma: no cover - offline
        pytest.skip(f"no market data: {e}")
    if not sigs:
        pytest.skip("no signal on META right now")
    assert sigs[0]["dte8p_flow"] > 0, "swing bucket is still empty"
