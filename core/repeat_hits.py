"""
repeat_hits.py — "DAY N": one contract, printing across more than one session.

A single day's unusual print is one decision, and the scanner already scores
it. The same ticker + type + strike + expiry printing again tomorrow, and
again the day after, is something else: a position being built over days. That
is the read the flow tab was missing, and it was unreachable — flow contracts
were computed, rendered, and thrown away, so no history ever accrued and the
badge could never light up however long the scanner ran.

SESSIONS, NOT SCANS
  The count is distinct trading days, so forty scans on a Tuesday is still
  DAY 1. Which day it is comes from the *exchange*, not the host: Railway runs
  UTC and the developer sits on Pacific, so a host date would roll over
  mid-afternoon in New York and split one session into two — manufacturing a
  DAY 2 out of a single day's flow, which is exactly the false positive this
  signal exists to avoid.

RECORD FIRST, THEN COUNT
  The write is an upsert on (session, contract), so re-running a scan cannot
  inflate the count. That makes record-then-count order-independent and lets
  the count include today: N is "sessions seen", so N=1 is a first sighting
  and gets no badge, and N>=2 is the signal.

Failure is never fatal here. The store is a convenience layered on top of
flow; a corrupt or unwritable database costs the day count and nothing else,
and every contract still leaves this module with a usable `repeat_days`.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional

from core.market_calendar import exchange_today
from data.baseline import BaselineStore

# The key stamped onto every flow contract dict. 1 means "first sighting" and
# the UI must not badge it; 2 and up is the DAY N the badge reads.
REPEAT_DAYS_KEY = "repeat_days"


class RepeatHitTracker:
    """Records a scan's flow contracts and stamps each with its session count.

    One instance per scan, created on the thread that runs it — a sqlite
    connection belongs to the thread that opened it, and the web scan runs on a
    worker thread, so a shared module-level store would raise on the second
    scan.
    """

    def __init__(self, store=None, session_date: Optional[_dt.date] = None):
        self.session = (session_date or exchange_today()).isoformat()
        self.store = store
        if self.store is None:
            try:
                self.store = BaselineStore()
            except Exception:
                self.store = None
        # Pruning is scan-level work, so it belongs in the constructor rather
        # than in annotate(), which runs once per contract.
        if self.store is not None:
            try:
                self.store.prune()
            except Exception:
                pass   # a store too broken to prune is handled by _sessions_for

    def annotate(self, signal: Dict) -> None:
        """Record every contract in `signal` and stamp its day count in place.

        Both sides, always — a put that has been accumulating for three days is
        the signal whether or not the ticker's flow is call-biased. `top_call`,
        `top_put` and `top_contract` are the same dict objects as the ones in
        these lists, so they carry the stamp without being touched.
        """
        for contract in self._contracts(signal):
            contract[REPEAT_DAYS_KEY] = self._sessions_for(contract)

    @staticmethod
    def _contracts(signal: Dict) -> List[Dict]:
        return [c for c in (list(signal.get("call_contracts") or [])
                            + list(signal.get("put_contracts") or []))
                if isinstance(c, dict)]

    def _sessions_for(self, contract: Dict) -> int:
        """Sessions this contract has appeared in, today included. 1 on doubt.

        Every failure — no store, an unkeyable contract, a database that has
        gone bad mid-scan — resolves to 1, which reads as "first sighting" and
        renders no badge. Claiming DAY 1 for a contract that is really on day
        four costs a signal; claiming DAY 4 for one we cannot verify would put
        a conviction read in front of a trade, which is worse.
        """
        if self.store is None:
            return 1
        ticker = contract.get("ticker")
        opt_type = contract.get("type")
        strike = contract.get("strike")
        expiry = contract.get("exp")
        if not (ticker and opt_type and expiry) or strike is None:
            return 1
        try:
            self.store.record_contract(
                self.session, ticker, opt_type, strike, expiry,
                oi=int(contract.get("oi") or 0),
                volume=int(contract.get("vol") or 0),
            )
            return max(1, self.store.contract_sessions(ticker, opt_type, strike, expiry))
        except Exception:
            # Stop hammering a store that has already failed once; the rest of
            # the scan should not pay a database timeout per contract.
            self.store = None
            return 1
