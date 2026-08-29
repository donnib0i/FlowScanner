"""Tests for sector constituent mapping and the diverging-stock laggard."""
import data.sector_constituents as sc
from core.scanner import top_individual_laggard, SECTOR_ETFS


# ── constituents_for ─────────────────────────────────────────────────────────
def test_constituents_excludes_etfs(monkeypatch):
    # Live map mixes an ETF (XLK) in with stocks — it must be filtered out.
    monkeypatch.setattr(sc, "get_ticker_sector_map", lambda: {
        "AAPL": "Information Technology",
        "MSFT": "Information Technology",
        "XLK":  "Information Technology",   # ETF — must not appear
        "JPM":  "Financials",
    })
    out = sc.constituents_for("Technology")
    assert "AAPL" in out and "MSFT" in out
    assert "XLK" not in out          # ETF filtered
    assert "JPM" not in out          # wrong sector (and not in Tech static)


def test_gics_and_yfinance_names_both_map(monkeypatch):
    # Live names (GICS + yfinance variants) are unioned with the static base.
    monkeypatch.setattr(sc, "get_ticker_sector_map", lambda: {
        "ZZZA": "Consumer Cyclical",        # yfinance name -> Discretionary
        "ZZZB": "Consumer Discretionary",   # GICS name
        "ZZZC": "Consumer Defensive",       # yfinance -> Staples
    })
    disc = sc.constituents_for("Consumer Discretionary")
    assert "ZZZA" in disc and "ZZZB" in disc
    assert "ZZZC" not in disc                       # that one is Staples
    assert "ZZZC" in sc.constituents_for("Consumer Staples")


def test_every_gics_sector_has_a_bucket():
    gics = {
        "Information Technology", "Communication Services", "Consumer Discretionary",
        "Consumer Staples", "Financials", "Health Care", "Energy", "Industrials",
        "Materials", "Utilities", "Real Estate",
    }
    for name in gics:
        assert sc.normalize_sector(name) in SECTOR_ETFS


def test_static_base_works_when_live_map_empty(monkeypatch):
    # Cloud scenario: live fetch IP-blocked -> still rich coverage from static base.
    monkeypatch.setattr(sc, "get_ticker_sector_map", lambda: {})
    fin = sc.constituents_for("Financials")
    assert len(fin) >= 30
    assert {"JPM", "GS", "BAC"} <= set(fin)
    # every static sector returns a healthy, ETF-free list
    for sector in SECTOR_ETFS:
        names = sc.constituents_for(sector)
        assert len(names) >= 25, f"{sector} too small: {len(names)}"
        assert "XLK" not in names and "SPY" not in names


# ── top_individual_laggard ───────────────────────────────────────────────────
def _stub_quotes(mapping):
    """Patch core.scanner._quotes_for to return canned change_pct per ticker."""
    def fake(tickers, period="5d"):
        return {t: {"change_pct": mapping[t], "dollar_vol": 1e6, "price": 10.0}
                for t in tickers if t in mapping}
    return fake


def test_laggard_is_red_name_in_green_sector(monkeypatch):
    import core.sectors as scn   # top_individual_laggard resolves names here
    monkeypatch.setattr(scn, "constituents_for",
                        lambda name, fallback_map=None: ["XOM", "CVX", "MP"])
    monkeypatch.setattr(scn, "_quotes_for",
                        _stub_quotes({"XOM": 1.8, "CVX": 1.2, "MP": -1.4}))
    sector_data = {"Energy": {"change_pct": 0.97}}
    lag = top_individual_laggard(sector_data)
    assert lag["ticker"] == "MP"
    assert lag["sector"] == "Energy"
    assert lag["divergence"] > 0
    assert lag["ticker"] not in scn.SECTOR_ETFS.values()  # never an ETF


def test_laggard_is_green_name_in_red_sector(monkeypatch):
    import core.sectors as scn   # top_individual_laggard resolves names here
    monkeypatch.setattr(scn, "constituents_for",
                        lambda name, fallback_map=None: ["AAPL", "MSFT", "NVDA"])
    monkeypatch.setattr(scn, "_quotes_for",
                        _stub_quotes({"AAPL": -2.0, "MSFT": -1.5, "NVDA": 1.1}))
    sector_data = {"Technology": {"change_pct": -1.2}}
    lag = top_individual_laggard(sector_data)
    assert lag["ticker"] == "NVDA"   # the green outlier in a red sector


def test_no_laggard_when_no_sector_moved():
    sector_data = {"Energy": {"change_pct": 0.1}, "Technology": {"change_pct": -0.2}}
    assert top_individual_laggard(sector_data) is None
