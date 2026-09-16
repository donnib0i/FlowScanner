"""
sector_map.py — what sector a ticker belongs to.

The scan showed "Other" for 522 of 612 names. The cause was not a bug in the
lookup but in its source: TICKER_SECTOR is a hand-maintained table of ~134
names, while the universe is rebuilt from live screeners every fifteen minutes
and runs to ~670. A hand-written map can never keep up with a list that changes
daily, and "Other" is not a sector -- it is the sector column not working, and
with it the grouping, the laggard ranking and the heatmap.

Three sources, cheapest first:

  1. TICKER_SECTOR      -- hand-curated, wins where it has an opinion
  2. STATIC_CONSTITUENTS -- the S&P constituent table, inverted (~478 names)
  3. a disk cache of answers previously resolved from yfinance

Only `warm()` touches the network. A scan calls `sector_for` with the default
network=False and gets an instant answer or None, because a scan that stops to
make 300 HTTP requests is a scan nobody runs twice.
"""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional

from core.constants import TICKER_SECTOR
from data.sector_constituents import STATIC_CONSTITUENTS, normalize_sector

_DEFAULT_CACHE = os.path.join(os.path.dirname(__file__), "sector_cache.json")

# STATIC_CONSTITUENTS is sector -> [tickers]; the scan needs the other direction.
_FROM_CONSTITUENTS: Dict[str, str] = {
    t: sector for sector, tickers in STATIC_CONSTITUENTS.items() for t in tickers
}

_cache: Optional[Dict[str, str]] = None
_cache_lock = threading.Lock()


def cache_path() -> str:
    """Env-overridable so the resolved answers can live on a mounted volume;
    a container's own filesystem is wiped on every deploy."""
    return os.environ.get("SCANNER_SECTOR_CACHE") or _DEFAULT_CACHE


def _load() -> Dict[str, str]:
    global _cache
    with _cache_lock:
        if _cache is None:
            try:
                with open(cache_path()) as fh:
                    loaded = json.load(fh)
                _cache = {k: v for k, v in loaded.items() if isinstance(v, str)}
            except Exception:
                _cache = {}
        return _cache


def _save() -> None:
    data = _load()
    try:
        path = cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=0, sort_keys=True)
        os.replace(tmp, path)          # never leave a half-written cache behind
    except Exception:
        pass


def sector_for(ticker: str, network: bool = False) -> Optional[str]:
    """The ticker's sector, or None when nothing knows it.

    None rather than "Other": the caller decides what to display, and a scan
    that cannot classify a name should say so rather than asserting a sector
    that does not exist.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return None
    hit = TICKER_SECTOR.get(t) or _FROM_CONSTITUENTS.get(t) or _load().get(t)
    if hit:
        return hit
    if not network:
        return None
    resolved = _lookup(t)
    if resolved:
        with _cache_lock:
            if _cache is not None:
                _cache[t] = resolved
        _save()
    return resolved


def _lookup(ticker: str) -> Optional[str]:
    """Ask yfinance. Slow, per-name, and the only part that can fail."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None
    raw = info.get("sector") or info.get("sectorDisp") or ""
    return normalize_sector(raw) if raw else None


def warm(tickers: Iterable[str], max_workers: int = 8,
         show_progress: bool = False) -> Dict[str, int]:
    """
    Resolve everything not already known, and write the cache once at the end.

    Run from a terminal, not from a request: this makes one HTTP call per
    unknown name.
    """
    todo: List[str] = []
    for t in tickers:
        u = (t or "").upper().strip()
        if u and sector_for(u) is None and u not in todo:
            todo.append(u)

    found = 0
    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for name, sector in zip(todo, pool.map(_lookup, todo)):
                if sector:
                    with _cache_lock:
                        if _cache is not None:
                            _cache[name] = sector
                    found += 1
                if show_progress:
                    print(f"  {name}: {sector or '—'}", flush=True)
        _save()
    return {"looked_up": len(todo), "resolved": found, "cached": len(_load())}


if __name__ == "__main__":       # pragma: no cover - operator tool
    import sys
    from core.universe import get_universe

    names = sys.argv[1:] or get_universe()
    print(f"Resolving sectors for {len(names)} tickers…")
    print(warm(names, show_progress="-v" in sys.argv))
