import os
os.environ.pop("SCANNER_PIN", None)  # disable auth for these tests
from fastapi.testclient import TestClient
import web.app as webapp

client = TestClient(webapp.app)


def test_plays_unknown_sector_404():
    r = client.get("/api/sector/NotASector/plays")
    assert r.status_code == 404


def test_plays_bad_dte_mode_400():
    r = client.get("/api/sector/Technology/plays?dte_mode=banana")
    assert r.status_code == 400


def test_plays_ok(monkeypatch):
    monkeypatch.setattr(webapp, "scan_sectors", lambda: {"Technology": {"change_pct": 1.5, "breakout": "up"}})
    monkeypatch.setattr(webapp, "fetch_vix", lambda: 15.0)
    monkeypatch.setattr(webapp, "sector_breakout_plays",
        lambda name, sd, vix, dte_mode: {"sector": name, "breakout": "up",
            "plays": [{"ticker": "AAA", "role": "laggard", "change": 0.1, "lag": 1.4,
                       "contract": {"label": "AAA up"}}]})
    r = client.get("/api/sector/Technology/plays?dte_mode=0dte")
    assert r.status_code == 200
    j = r.json()
    assert j["breakout"] == "up" and j["plays"][0]["ticker"] == "AAA"
