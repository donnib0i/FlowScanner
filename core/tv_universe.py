"""
tv_universe.py — TradingView screener CSV as a universe source.

core/universe.py builds its own universe from live screeners and index
membership. This module is the alternative: the user's own TradingView screener
already encodes filters and a ranking he trusts more than anything we could
reconstruct, so when he points us at an export our job is to carry that list
through faithfully — same symbols, same order — not to second-guess it.

That principle drives three decisions that differ from build_universe():

  * ETFs are kept. build_universe() strips them because its own sources drag in
    hundreds of funds nobody asked for. A fund in a TradingView export is there
    because the user's screener put it there.
  * Order is preserved exactly. The screener's sort *is* the ranking, and
    re-sorting would throw away the most useful thing in the file.
  * Nothing is dropped quietly. Every row we refuse is recorded with a reason so
    the caller can say "12 of 60 dropped: no options chain" instead of letting
    the user believe we scanned his whole list.

On the format itself: TradingView writes human-readable column *labels*, not
stable slugs, and both the label text and the set of columns change with the
user's chosen columns and UI locale. So we never index by position or trust a
fixed schema. We locate the ticker column by name with a positional fallback,
interpret only the numeric columns we recognize with confidence, and ignore the
rest rather than guessing at their meaning — a mislabeled "Volume" that is
actually average volume would poison ranking far worse than having no volume.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# A TradingView screener can easily return thousands of rows, and the scanner
# batch-downloads a year of history per symbol. Cap by default so that pointing
# at an unfiltered export does not turn into a ten-minute download; the cap is
# applied last, after ordering, so the user keeps his screener's top names.
DEFAULT_CAP = 250

# ── Column identification ─────────────────────────────────────────────────────
# Header aliases are matched against a normalized form of the label (lowercased,
# whitespace collapsed, parenthetical qualifiers such as "(10 day)" removed).
# The non-English entries cover the locales TradingView actually ships headers
# in; anything we do not recognize is reported as ignored rather than guessed.

# Tried in this order, not in file order: an export whose first column is
# "Description" and whose second is "Symbol" must still resolve to the second.
# Deliberately excluded is "Name" — in a TradingView export that column holds
# company names, and treating it as the ticker column would look like it worked
# while quietly producing nothing.
_TICKER_ALIASES = (
    "symbol", "ticker", "symbole", "símbolo", "simbolo",
    "código", "codigo", "kürzel", "kurzel",
)

# Canonical metadata field -> header aliases. Order within this mapping matters
# only for reporting; matching itself is exact against the normalized label, so
# "relative volume" can never be swallowed by the "volume" entry.
_METADATA_ALIASES: Dict[str, Tuple[str, ...]] = {
    "price":        ("price", "last", "last price", "close", "precio", "prix", "preis"),
    "volume":       ("volume", "vol", "volumen", "volumen total"),
    "relative_volume": ("relative volume", "rel volume", "rel vol", "rvol",
                        "relative volume at time", "volumen relativo"),
    "market_cap":   ("market cap", "market capitalization", "mkt cap", "mcap",
                     "market cap basic", "capitalizacion de mercado",
                     "capitalización de mercado"),
    "change_pct":   ("change %", "chg %", "% change", "change percent",
                     "change % 1d", "chg% 1d", "cambio %", "variation %"),
}

_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")
_WHITESPACE = re.compile(r"\s+")

# A US equity ticker after prefix-stripping: letters first, then letters/digits,
# with an optional class suffix (BRK.B / BRK-B). Deliberately anchored and
# short — it is also the fallback detector for "which column holds tickers", so
# it has to reject company names, sectors, and formatted numbers.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z]{1,2})?$")

_MULTIPLIERS = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _normalize_header(label: str) -> str:
    """Collapse a TradingView column label to a comparable key."""
    text = label.replace("﻿", "").strip().lower()
    text = _PARENTHETICAL.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def normalize_symbol(raw: str) -> Optional[str]:
    """
    Turn one TradingView cell into a scanner ticker, or None if it is not one.

    TradingView writes symbols either bare ("AAPL") or exchange-qualified
    ("NASDAQ:AAPL"), and uses a dot for share classes where yfinance — and
    therefore the rest of this scanner, including fetch_sp500() — uses a hyphen.
    Both are normalized here so the caller never has to care which form the
    export used.
    """
    if not isinstance(raw, str):
        return None
    text = raw.replace("﻿", "").strip().strip('"').upper()
    if not text:
        return None
    # Exchange prefix: keep the part after the last colon ("NASDAQ:AAPL").
    if ":" in text:
        text = text.rsplit(":", 1)[-1].strip()
    text = text.replace(".", "-")
    if not _TICKER_RE.match(text):
        return None
    return text


def _looks_like_ticker_column(values: Sequence[str]) -> float:
    """Fraction of non-blank cells in a column that parse as tickers."""
    filled = [v for v in values if isinstance(v, str) and v.strip()]
    if not filled:
        return 0.0
    return sum(1 for v in filled if normalize_symbol(v)) / len(filled)


def parse_number(raw: str, decimal_comma: bool = False) -> Optional[float]:
    """
    Parse a TradingView numeric cell, or None if it is not confidently a number.

    Handles the decorations TradingView applies to numbers in the UI and carries
    into the export: currency symbols, thousands separators, percent signs, the
    Unicode minus, and K/M/B/T magnitude suffixes ("1.23B"). Anything left over
    after stripping those means we did not understand the cell, and we return
    None rather than a half-parsed number.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().strip('"')
    if not text or text in {"—", "-", "–", "N/A", "n/a", "null"}:
        return None
    text = text.replace("−", "-").replace("–", "-")  # Unicode minus / en dash
    text = re.sub(r"[^\d.,\-+KMBTkmbt%]", "", text)  # drop $, €, spaces, etc.
    text = text.replace("%", "").replace("+", "")
    if not text:
        return None

    multiplier = 1.0
    if text and text[-1] in "KMBTkmbt":
        multiplier = _MULTIPLIERS[text[-1].upper()]
        text = text[:-1]

    if decimal_comma:
        # A semicolon-delimited export is a European locale one, where the comma
        # is the decimal mark and the dot (or a space, already stripped) groups
        # thousands. We only apply this when the delimiter told us so — guessing
        # from the digits alone turns "1,234" into either 1234 or 1.234.
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")

    try:
        return float(text) * multiplier
    except ValueError:
        return None


@dataclass
class TradingViewUniverse:
    """
    The result of reading one TradingView export.

    `symbols` is the universe in the screener's own order. `metadata` carries
    only the numeric columns we could identify, keyed by ticker then by
    canonical field name, for a later ranking pass that may want them. The
    remaining fields exist so the caller can tell the user what happened to his
    file rather than presenting a silently shortened list.
    """
    symbols: List[str] = field(default_factory=list)
    metadata: Dict[str, Dict[str, float]] = field(default_factory=dict)
    source: str = ""
    ticker_column: str = ""
    metadata_columns: Dict[str, str] = field(default_factory=dict)
    ignored_columns: List[str] = field(default_factory=list)
    data_rows: int = 0
    blank_rows: int = 0
    unparsed: List[Tuple[int, str]] = field(default_factory=list)
    duplicates: List[str] = field(default_factory=list)
    capped_from: int = 0

    def summary(self) -> str:
        """One-line description of what came out of the file."""
        parts = [f"{len(self.symbols)} symbols from {self.source or 'TradingView CSV'}"]
        if self.capped_from:
            parts.append(f"capped from {self.capped_from}")
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} duplicate(s) removed")
        if self.unparsed:
            parts.append(f"{len(self.unparsed)} row(s) unreadable")
        if self.metadata_columns:
            parts.append("metadata: " + ", ".join(sorted(self.metadata_columns)))
        return " | ".join(parts)

    def detail_lines(self) -> List[str]:
        """
        Everything worth telling the user that did not fit on the summary line.

        Ignored columns are surfaced deliberately: if the user selected a column
        he expects us to rank on and it does not appear here, that is the signal
        that we did not understand its label.
        """
        lines: List[str] = []
        if self.ticker_column:
            lines.append(f"ticker column: {self.ticker_column!r}")
        if self.ignored_columns:
            lines.append("columns ignored (unrecognized): "
                         + ", ".join(repr(c) for c in self.ignored_columns))
        if self.unparsed:
            shown = ", ".join(f"row {n}: {v!r}" for n, v in self.unparsed[:5])
            more = f" (+{len(self.unparsed) - 5} more)" if len(self.unparsed) > 5 else ""
            lines.append(f"unreadable rows: {shown}{more}")
        if self.duplicates:
            lines.append("duplicates dropped: " + ", ".join(self.duplicates[:10]))
        return lines


def _sniff_delimiter(sample: str) -> str:
    """
    Pick the delimiter for a TradingView export.

    csv.Sniffer is fine on well-formed files but happily returns nonsense on a
    single-column export, so we decide by counting candidates on the header line
    and only fall back to a comma. Semicolon exports are the European locale
    variant, which also implies a decimal comma downstream.
    """
    header = sample.splitlines()[0] if sample.splitlines() else ""
    counts = {d: header.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def parse_tradingview_csv(text: str, source: str = "",
                          cap: Optional[int] = DEFAULT_CAP) -> TradingViewUniverse:
    """
    Parse the text of a TradingView screener export into a universe.

    Kept separate from file reading so tests can feed strings and so a future
    caller can hand us a paste of the clipboard instead of a path.
    """
    text = text.lstrip("﻿")
    result = TradingViewUniverse(source=source)
    if not text.strip():
        return result

    delimiter = _sniff_delimiter(text)
    decimal_comma = delimiter == ";"
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]

    # TradingView exports occasionally carry a blank leading line, and always a
    # header. Skip forward to the first row with any content and treat it as the
    # header — the file has no other way of telling us where the table starts.
    header: List[str] = []
    body_start = 0
    for i, row in enumerate(rows):
        if any(cell.strip() for cell in row):
            header = row
            body_start = i + 1
            break
    if not header:
        return result

    body = rows[body_start:]
    normalized = [_normalize_header(h) for h in header]

    # ── Locate the ticker column ──────────────────────────────────────────────
    def _column(i: int) -> List[str]:
        return [row[i] for row in body if i < len(row)]

    ticker_idx: Optional[int] = None
    for alias in _TICKER_ALIASES:
        for i, key in enumerate(normalized):
            # A matching label is strong evidence but not proof — a localized
            # export can reuse a word we mapped, so the column still has to hold
            # things that parse as tickers before we commit to it.
            if key == alias and _looks_like_ticker_column(_column(i)) >= 0.5:
                ticker_idx = i
                break
        if ticker_idx is not None:
            break
    if ticker_idx is None:
        # No usable label — most likely a locale we have no alias for. Fall back
        # to content: score every column by how many of its cells actually look
        # like tickers and take the best, requiring a clear majority so we never
        # mistake a sector or description column for symbols.
        best_score = 0.0
        for i in range(len(header)):
            score = _looks_like_ticker_column(_column(i))
            if score > best_score:
                best_score, ticker_idx = score, i
        if ticker_idx is None or best_score < 0.6:
            return result
    result.ticker_column = header[ticker_idx].strip()

    # ── Identify the numeric columns we understand ────────────────────────────
    # First match wins per canonical field: a second "Volume"-ish column is more
    # likely to be a different measure (average volume) than a better version of
    # the same one, so it goes to ignored rather than overwriting.
    column_fields: Dict[int, str] = {}
    for i, key in enumerate(normalized):
        if i == ticker_idx:
            continue
        matched = None
        for field_name, aliases in _METADATA_ALIASES.items():
            if key in aliases and field_name not in result.metadata_columns:
                matched = field_name
                break
        if matched:
            column_fields[i] = matched
            result.metadata_columns[matched] = header[i].strip()
        elif header[i].strip():
            result.ignored_columns.append(header[i].strip())

    # ── Read the rows ─────────────────────────────────────────────────────────
    seen: set = set()
    ordered: List[str] = []
    for offset, row in enumerate(body):
        line_no = body_start + offset + 1  # 1-based, as a text editor shows it
        if not any(cell.strip() for cell in row):
            result.blank_rows += 1
            continue
        result.data_rows += 1
        raw = row[ticker_idx] if ticker_idx < len(row) else ""
        symbol = normalize_symbol(raw)
        if symbol is None:
            result.unparsed.append((line_no, raw.strip()))
            continue
        if symbol in seen:
            result.duplicates.append(symbol)
            continue
        seen.add(symbol)
        ordered.append(symbol)

        values: Dict[str, float] = {}
        for i, field_name in column_fields.items():
            if i >= len(row):
                continue
            parsed = parse_number(row[i], decimal_comma=decimal_comma)
            if parsed is not None:
                values[field_name] = parsed
        if values:
            result.metadata[symbol] = values

    if cap is not None and cap > 0 and len(ordered) > cap:
        result.capped_from = len(ordered)
        dropped = ordered[cap:]
        ordered = ordered[:cap]
        for sym in dropped:
            result.metadata.pop(sym, None)

    result.symbols = ordered
    return result


def load_tradingview_csv(path: str, cap: Optional[int] = DEFAULT_CAP) -> TradingViewUniverse:
    """
    Read a TradingView export from disk.

    utf-8-sig strips the BOM TradingView writes so Excel opens the file cleanly;
    errors="replace" keeps a stray non-UTF-8 byte from killing an otherwise fine
    600-row list, since a mangled character in a column we ignore is harmless.
    """
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        return parse_tradingview_csv(f.read(), source=path, cap=cap)


def drop_report(requested: Sequence[str], survived: Sequence[str], reason: str) -> str:
    """
    Describe what the data layer could not handle, in the user's own terms.

    This exists because silent shrinkage is the specific failure that makes a
    user stop trusting the tool: he exports 60 names, the scanner shows 48, and
    with no explanation the only available conclusion is that his list was
    ignored. Returns "" when nothing was lost, so callers can print it blindly.
    """
    kept = set(survived)
    missing = [s for s in requested if s not in kept]
    if not missing:
        return ""
    shown = ", ".join(missing[:12])
    more = f" (+{len(missing) - 12} more)" if len(missing) > 12 else ""
    return (f"{len(missing)} of {len(requested)} symbols dropped: "
            f"{reason} — {shown}{more}")
