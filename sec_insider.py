#!/usr/bin/env python3
# sec_insider.py — SEC EDGAR Form 4 insider buy/sell tracker
# No API key needed. Free public data from SEC EDGAR.
# Rate limit: 10 req/sec max — sleeps 0.1s between requests.

import requests
import xml.etree.ElementTree as ET
import time
import logging
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional
from functools import lru_cache

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

logger = logging.getLogger(__name__)

# SEC requires a descriptive User-Agent or you'll get 403s
HEADERS = {
    "User-Agent": "AiAgent scanner dantefernandez626@gmail.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json, application/xml, text/xml, */*",
}

# ── cache ──────────────────────────────────────────────────────────────────────
_insider_cache: Dict = {}
_insider_cache_ts: float = 0.0
INSIDER_CACHE_TTL = 1800  # 30 minutes


# ── helpers ───────────────────────────────────────────────────────────────────

def _sleep():
    """Polite EDGAR delay — stay under 10 req/sec."""
    time.sleep(0.12)


def _get(url: str, timeout: int = 15) -> Optional[requests.Response]:
    """GET with EDGAR-required headers. Returns None on any error."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return resp
        logger.debug("EDGAR %s → HTTP %s", url, resp.status_code)
        return None
    except Exception as exc:
        logger.debug("EDGAR request failed: %s — %s", url, exc)
        return None


def _parse_form4_xml(xml_text: str) -> List[Dict]:
    """
    Parse a single Form 4 XML document.
    Returns a list of transaction dicts (one per nonDerivativeTransaction row).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug("XML parse error: %s", e)
        return []

    ns = {"": ""}  # Form 4 has no namespace

    def _find(node, *paths):
        """Try multiple tag paths, return first non-None text."""
        for path in paths:
            el = node.find(path)
            if el is not None and el.text:
                return el.text.strip()
        return ""

    # ── issuer info ────────────────────────────────────────────────────────────
    issuer_ticker = _find(root, "issuer/issuerTradingSymbol")

    # ── reporting owner ────────────────────────────────────────────────────────
    owner_name = _find(
        root,
        "reportingOwner/reportingOwnerId/rptOwnerName",
        "reportingOwner/reportingOwnerIdentity/rptOwnerName",
    )
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    title = ""
    is_officer = False
    is_director = False
    if rel is not None:
        title_el = rel.find("officerTitle")
        if title_el is not None and title_el.text:
            title = title_el.text.strip()
        is_officer = (rel.findtext("isOfficer", "0") or "0").strip() == "1"
        is_director = (rel.findtext("isDirector", "0") or "0").strip() == "1"
        if not title and is_director:
            title = "Director"
        elif not title and is_officer:
            title = "Officer"

    transactions = []

    # ── non-derivative transactions (direct stock buys/sells) ─────────────────
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        # transaction date
        tx_date = _find(tx, "transactionDate/value")
        if not tx_date:
            continue

        # acquired (A) or disposed (D)
        code_val = _find(
            tx,
            "transactionAmounts/transactionAcquiredDisposedCode/value",
        )
        if code_val not in ("A", "D"):
            continue
        tx_type = "P" if code_val == "A" else "S"

        # shares
        shares_str = _find(tx, "transactionAmounts/transactionShares/value")
        price_str = _find(
            tx, "transactionAmounts/transactionPricePerShare/value"
        )

        try:
            shares = float(shares_str) if shares_str else 0.0
        except ValueError:
            shares = 0.0
        try:
            price = float(price_str) if price_str else 0.0
        except ValueError:
            price = 0.0

        transactions.append(
            {
                "issuer_ticker": issuer_ticker,
                "insider_name": owner_name,
                "title": title,
                "is_officer": is_officer,
                "is_director": is_director,
                "transaction_type": tx_type,
                "shares": shares,
                "price": price,
                "value": shares * price,
                "date": tx_date,
            }
        )

    return transactions


# ── EDGAR REST API (data.sec.gov) — ticker → CIK mapping ─────────────────────

_ticker_cik_map: Dict[str, str] = {}
_ticker_cik_ts: float = 0.0
_TICKER_CIK_TTL = 86400  # refresh once a day

def _get_ticker_cik_map() -> Dict[str, str]:
    """Download company_tickers.json and return {TICKER: CIK_10_PADDED}."""
    global _ticker_cik_map, _ticker_cik_ts
    now = time.time()
    if _ticker_cik_map and (now - _ticker_cik_ts) < _TICKER_CIK_TTL:
        return _ticker_cik_map
    resp = _get("https://www.sec.gov/files/company_tickers.json")
    if resp is None:
        return _ticker_cik_map  # return stale
    try:
        data = resp.json()
        mapping = {}
        for entry in data.values():
            ticker = str(entry.get("ticker", "")).upper()
            cik = str(entry.get("cik_str", ""))
            if ticker and cik:
                mapping[ticker] = cik.zfill(10)
        _ticker_cik_map = mapping
        _ticker_cik_ts = now
        logger.debug("CIK map loaded: %d companies", len(mapping))
    except Exception as exc:
        logger.warning("Failed to load CIK map: %s", exc)
    return _ticker_cik_map


def _get_form4_xml_urls(ticker: str, cik: str, cutoff: datetime.date, max_filings: int = 10) -> List[str]:
    """
    Use data.sec.gov/submissions/CIK{cik}.json to get recent Form 4 XML URLs.
    Returns list of direct XML archive URLs.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = _get(url)
    _sleep()
    if resp is None:
        return []

    try:
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accs = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        cik_raw = str(data.get("cik", cik)).lstrip("0") or cik.lstrip("0")
    except Exception as exc:
        logger.debug("submissions JSON parse failed for %s: %s", ticker, exc)
        return []

    xml_urls = []
    for i, form in enumerate(forms):
        if form != "4":
            continue
        if i >= len(dates) or i >= len(accs):
            continue
        try:
            filing_date = datetime.date.fromisoformat(dates[i])
        except ValueError:
            continue
        if filing_date < cutoff:
            break  # filings are newest-first; stop once past cutoff
        acc_clean = accs[i].replace("-", "")
        # Get the raw XML filename: primaryDocument may have an XSLT wrapper prefix
        # e.g. "xslF345X06/form4.xml" → we want "form4.xml"
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        xml_filename = primary_doc.split("/")[-1] if primary_doc else "form4.xml"
        if not xml_filename.endswith(".xml"):
            xml_filename = "form4.xml"
        xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{acc_clean}/{xml_filename}"
        xml_urls.append(xml_url)
        if len(xml_urls) >= max_filings:
            break

    return xml_urls


# ── public API ────────────────────────────────────────────────────────────────

def get_insider_filings(ticker: str, days_back: int = 30) -> List[Dict]:
    """
    Return Form 4 transaction dicts for ``ticker`` in the last ``days_back`` days.
    Uses data.sec.gov REST API — faster and more reliable than CGI.
    """
    ticker = ticker.upper()
    cutoff = datetime.date.today() - datetime.timedelta(days=days_back)
    results: List[Dict] = []

    try:
        cik_map = _get_ticker_cik_map()
        cik = cik_map.get(ticker)
        if not cik:
            logger.debug("%s: CIK not found in EDGAR company tickers", ticker)
            return []

        xml_urls = _get_form4_xml_urls(ticker, cik, cutoff)
        if not xml_urls:
            return []

        for xml_url in xml_urls:
            resp = _get(xml_url)
            _sleep()
            if resp is None:
                continue
            if "<ownershipDocument>" not in resp.text:
                continue

            raw_txs = _parse_form4_xml(resp.text)
            for tx in raw_txs:
                try:
                    tx_date = datetime.date.fromisoformat(tx["date"])
                except ValueError:
                    continue
                if tx_date < cutoff:
                    continue
                days_ago = (datetime.date.today() - tx_date).days
                results.append({
                    "ticker": ticker,
                    "insider_name": tx["insider_name"],
                    "title": tx["title"],
                    "transaction_type": tx["transaction_type"],
                    "shares": tx["shares"],
                    "price": tx["price"],
                    "value": tx["value"],
                    "date": tx["date"],
                    "days_ago": days_ago,
                })

    except Exception as exc:
        logger.warning("get_insider_filings(%s) error: %s", ticker, exc)

    results.sort(key=lambda x: x["date"], reverse=True)
    return results


def _compute_score(filings: List[Dict]) -> float:
    """
    Score formula:
      Each buy in last 30 days:       +10 (capped at 60 from buys)
      Buy value > $100k:              +10 bonus
      Buy value > $1M:                +20 bonus (stacks with 100k bonus)
      CEO/President buying (any buy): +10 bonus (once)
      Each sell:                      -5
      Final: clamp 0–100
    """
    score = 0.0
    buy_points = 0.0
    ceo_bonus_given = False

    for f in filings:
        if f["transaction_type"] == "P":
            buy_points += 10
            val = f["value"]
            if val > 1_000_000:
                score += 20
            if val > 100_000:
                score += 10
            title_lower = f["title"].lower()
            if not ceo_bonus_given and any(
                kw in title_lower for kw in ("ceo", "chief executive", "president")
            ):
                score += 10
                ceo_bonus_given = True
        else:
            score -= 5

    score += min(buy_points, 60)
    return min(100.0, max(0.0, score))


def get_insider_signals(tickers: List[str], days_back: int = 30) -> List[Dict]:
    """
    Aggregate Form 4 data for multiple tickers and return signals.

    Each signal dict:
    {
        "ticker": str,
        "buy_count": int,
        "sell_count": int,
        "buy_value": float,
        "sell_value": float,
        "net_sentiment": str,   # "BUYING", "SELLING", "MIXED", "QUIET"
        "score": float,         # 0–100, higher = more bullish insider activity
        "latest_buy": str,      # date of most recent buy (YYYY-MM-DD) or ""
        "recent_filings": List[Dict],  # last 5 filings
    }
    """
    def _process_ticker(ticker: str) -> Dict:
        try:
            filings = get_insider_filings(ticker, days_back=days_back)
            buys = [f for f in filings if f["transaction_type"] == "P"]
            sells = [f for f in filings if f["transaction_type"] == "S"]
            buy_value = sum(f["value"] for f in buys)
            sell_value = sum(f["value"] for f in sells)
            bc, sc = len(buys), len(sells)
            if bc == 0 and sc == 0:
                sentiment = "QUIET"
            elif bc > 0 and sc == 0:
                sentiment = "BUYING"
            elif sc > 0 and bc == 0:
                sentiment = "SELLING"
            else:
                sentiment = "BUYING" if bc >= sc * 2 else (
                    "SELLING" if sc >= bc * 2 else "MIXED"
                )
            return {
                "ticker": ticker,
                "buy_count": bc,
                "sell_count": sc,
                "buy_value": buy_value,
                "sell_value": sell_value,
                "net_sentiment": sentiment,
                "score": _compute_score(filings),
                "latest_buy": buys[0]["date"] if buys else "",
                "recent_filings": filings[:5],
            }
        except Exception as exc:
            logger.warning("get_insider_signals(%s) error: %s", ticker, exc)
            return {
                "ticker": ticker, "buy_count": 0, "sell_count": 0,
                "buy_value": 0.0, "sell_value": 0.0,
                "net_sentiment": "QUIET", "score": 0.0,
                "latest_buy": "", "recent_filings": [],
            }

    # SEC rate limit: 10 req/sec — 5 workers × ~0.12s sleep ≈ safe
    signals: List[Dict] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_process_ticker, t): t for t in tickers}
        for future in as_completed(futures):
            signals.append(future.result())

    return signals


# ── cached version ─────────────────────────────────────────────────────────────

def get_insider_signals_cached(tickers: List[str], days_back: int = 30) -> List[Dict]:
    """
    Wraps get_insider_signals with a 30-minute in-process cache keyed on
    (frozenset(tickers), days_back).
    """
    global _insider_cache, _insider_cache_ts

    now = time.time()
    cache_key = (frozenset(t.upper() for t in tickers), days_back)

    if (
        _insider_cache_ts
        and now - _insider_cache_ts < INSIDER_CACHE_TTL
        and cache_key in _insider_cache
    ):
        logger.debug("insider cache HIT for %s", cache_key)
        return _insider_cache[cache_key]

    logger.debug("insider cache MISS for %s — fetching", cache_key)
    signals = get_insider_signals(tickers, days_back=days_back)

    _insider_cache[cache_key] = signals
    _insider_cache_ts = now
    return signals


# ── display ───────────────────────────────────────────────────────────────────

def format_insider_table(signals: List[Dict]) -> str:
    """
    Returns a tabulate table string for signals with score > 20.
    Falls back to a plain text summary if tabulate is not installed.
    """
    filtered = [s for s in signals if s["score"] > 20]
    if not filtered:
        return "No notable insider activity (score > 20) found."

    filtered.sort(key=lambda x: x["score"], reverse=True)

    rows = []
    for s in filtered:
        buy_val_str = (
            f"${s['buy_value']:,.0f}" if s["buy_value"] else "-"
        )
        sell_val_str = (
            f"${s['sell_value']:,.0f}" if s["sell_value"] else "-"
        )
        rows.append(
            [
                s["ticker"],
                s["net_sentiment"],
                s["buy_count"],
                s["sell_count"],
                buy_val_str,
                sell_val_str,
                f"{s['score']:.0f}",
                s["latest_buy"] or "-",
            ]
        )

    headers = [
        "Ticker", "Sentiment", "Buys", "Sells",
        "Buy $", "Sell $", "Score", "Latest Buy",
    ]

    if HAS_TABULATE:
        return tabulate(rows, headers=headers, tablefmt="simple")
    else:
        # Manual fallback
        col_widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
        fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
        lines = [fmt.format(*headers)]
        lines.append("  ".join("-" * w for w in col_widths))
        for row in rows:
            lines.append(fmt.format(*row))
        return "\n".join(lines)


# ── CLI quick-test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    tickers = sys.argv[1:] if len(sys.argv) > 1 else ["AAPL", "NVDA", "TSLA"]
    print(f"\nFetching insider Form 4 data for: {', '.join(tickers)}\n")

    signals = get_insider_signals(tickers, days_back=30)

    print(format_insider_table(signals))
    print()

    # Verbose per-ticker breakdown
    for sig in signals:
        print(
            f"[{sig['ticker']}] score={sig['score']:.0f}  "
            f"sentiment={sig['net_sentiment']}  "
            f"buys={sig['buy_count']} (${sig['buy_value']:,.0f})  "
            f"sells={sig['sell_count']} (${sig['sell_value']:,.0f})"
        )
        for f in sig["recent_filings"][:3]:
            print(
                f"  {f['date']}  {f['transaction_type']}  "
                f"{f['insider_name']} ({f['title']})  "
                f"{f['shares']:,.0f} sh @ ${f['price']:.2f}  "
                f"= ${f['value']:,.0f}"
            )
