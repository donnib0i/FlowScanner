# Dealer Gamma Exposure — Full Greeks Engine + Measured GEX Surface

**Date:** 2026-09-06
**Status:** Approved design, pending spec review

## Goal

Compute the full Black-Scholes Greeks locally and aggregate strike-level gamma
into a **dealer gamma exposure surface** for SPX/QQQ: net gamma notional per
strike, the zero-gamma flip level, and the call/put walls.

The scanner currently computes `bs_delta` and nothing else. `data/sources.py`
ingests gamma/theta/vega from Tradier and Polygon when those feeds are
configured, but no code consumes them, and the default yfinance path supplies
none. There is no gamma aggregation anywhere in the repo. This is greenfield.

## Prime directive: measurement, not prediction

Every output of this subsystem is **a measured quantity with units and a stated
derivation**. The module reports what the chain says dealers are holding. It
does not say where price is going.

This is a hard constraint on the design, not a stylistic preference:

- **No composite GEX score.** No 0–100 number.
- **No regime verdict.** No "pin day", "trend day", "bias", "target", or any
  other forecast label anywhere in the module, the API response, or the UI.
- **No feeding `_score_contract` or `calc_whale_score`.** Greeks stay
  informational. Contract ranking is calibrated against existing signals and
  does not change in this work.
- **Provenance is mandatory, not optional.** Every response states how stale
  its inputs are and how much of the profile was observed versus assumed.

Rationale: the arithmetic is exact, but the inference from dealer inventory to
a price path is not. Dealers hedge late, hedge in another product, or run an
already-flat book. Open interest from yfinance is prior-day *settled*, so the
map is stalest on exactly the days with the largest overnight repositioning. A
number printed to two decimals reads as certainty; certainty sizes up and holds
through the stop. The module's job is to resist that reading.

## Non-goals

- Risk-neutral density / distribution entropy (phase 2; reuses this chain fetch).
- Any change to contract scoring or ranking weights.
- Vanna and charm *surfaces* — the per-contract values are computed and
  exposed, but not aggregated into their own profiles.
- Historical GEX time series or intraday GEX replay.
- Chart libraries. The UI is hand-rolled SVG, matching `web/static/app.js`.

---

## Component 1: `core/greeks.py` — pure option math

New module. No I/O, no pandas, no network. Fully unit-testable in isolation.

Takes ownership of `norm_cdf` and `bs_delta`, which move here from
`core/options.py`. `core/options.py` imports and re-exports both, so
`core.scanner`'s public surface is unchanged and every existing call site keeps
working.

### API

```python
norm_cdf(x) -> float          # moved from options.py, unchanged
norm_pdf(x) -> float          # new
bs_delta(S, K, T, sigma, opt_type="call") -> float   # moved, unchanged

bs_greeks(S, K, T, sigma, r=0.0, opt_type="call") -> dict
    # {delta, gamma, vega, theta, vanna, charm}

implied_vol(price, S, K, T, r=0.0, opt_type="call") -> float | None
```

`r` defaults to `0.0` everywhere. The existing `bs_delta` implicitly assumes a
zero rate, and every calibrated signal in the repo inherits that assumption.
The parameter is exposed but the default must not change, or previously
calibrated deltas shift underneath signals that were tuned against them. For
0DTE the rate term is negligible regardless.

### Near-expiry gamma handling

`bs_delta` already handles `T → 0` by switching to the digital limit below
`T < 0.0001`. Gamma has the opposite pathology: it diverges to infinity at the
money rather than converging to a limit.

On a 0DTE SPX chain at 15:50 ET, a naive gamma on the ATM strike returns a
value that swamps the rest of the profile and makes the flip level meaningless.
Mitigation, in order:

1. Floor `T` at one minute (`1 / (365 * 1440)`), matching `bs_delta`.
2. Floor `sigma` at `0.05`, matching `bs_delta`.
3. After computing gamma notional for every strike in a chain, clamp any strike
   exceeding `GEX_GAMMA_CLAMP_X` times the chain's *median* nonzero gamma
   notional down to that ceiling.
4. Count clamped strikes and surface the count in provenance.

Median, not mean — the mean is itself dragged by the outlier being clamped.

This is the single most likely source of a wrong flip level and gets explicit
test coverage.

### Implied volatility back-solve

yfinance returns `impliedVolatility` of `0.0` or `NaN` for a meaningful share
of far-OTM SPX strikes — precisely the wing strikes carrying the largest open
interest. Zero IV yields zero gamma, which silently deletes those strikes from
the profile and biases the flip level toward the money.

`implied_vol()` back-solves from the contract mid price:

- Newton-Raphson on vega, seeded at `0.20`, capped at 50 iterations.
- Bisection fallback over `[0.01, 5.0]` when Newton diverges or vega underflows
  (which it does deep OTM — the common case here, so the fallback is the
  expected path, not an edge case).
- Returns `None` when the price is below intrinsic, non-positive, or neither
  method converges.

Strikes where both the feed and the solver fail are **excluded from the profile
and counted**, and that count appears in provenance. Silent exclusion is the
failure mode being designed against.

---

## Component 2: `core/gex.py` — aggregation

### Convention

Stated explicitly in the module docstring and in the API response, because
vendors disagree and the reader needs to know which one they are looking at:

```
gamma_notional(K) = gamma(K) * OI(K) * 100 * S^2 * 0.01
```

Units: **dollars of delta that dealers must hedge per 1% move in spot.**
Sign: positive = dealers long gamma at that strike, negative = short.

### Dealer sign — hybrid inference

The sign convention *is* the model. The standard industry assumption (dealers
long calls, short puts) is known to be wrong on index products. The scanner
already classifies trade side via `classify_trade_side`, so the real sign is
partially observable — an advantage most vendors do not have.

Per strike:

1. Aggregate today's flow records at that strike through `classify_trade_side`.
   Ask-side volume = customer bought = **dealer short gamma**. Bid-side volume
   = customer sold = **dealer long gamma**. Mid-side is discarded, not split.
2. If classified volume at the strike clears **both** gates —
   `GEX_INFER_MIN_CONTRACTS` in absolute terms and `GEX_INFER_MIN_SHARE` as a
   fraction of that strike's total volume — take the inferred sign, weighted by
   how lopsided the split is. Tag the strike `src: "inferred"` with its
   confidence (the winning side's share).
3. Otherwise fall back to the naive convention on settled OI. Tag the strike
   `src: "assumed"`.

Two gates, not one: a strike with 3 contracts all on the ask is 100% lopsided
and carries no information. The absolute floor rejects it.

The response reports the overall inferred/assumed split by gamma notional (not
by strike count — a handful of high-OI strikes dominate the profile), so the UI
can state how much of the surface was actually observed.

### Zero-gamma flip — grid method

The common implementation reads the flip off the strike-axis bars, interpolating
between the last positive and first negative bar. That is wrong: it conflates
"net gamma contributed by strike K" with "net gamma when spot is at K." Those
are different functions and their roots differ, sometimes by a lot.

Correct method:

1. Build a grid of hypothetical spot prices spanning `±GEX_GRID_PCT` around
   current spot, `GEX_GRID_STEPS` points.
2. At each grid spot `S'`, recompute **every** strike's gamma at `S'` (gamma is
   a function of moneyness, so the whole profile moves) and sum the signed
   notional.
3. Find sign changes in the resulting curve and bisect to
   `GEX_FLIP_TOLERANCE` index points.
4. Report **all** roots found, not just the first. A profile with multiple
   crossings is a real and informative state; collapsing it to one number
   discards that. `flip` is the root nearest spot; `flips` is the full list.
5. Report `None` when the curve does not cross zero within the grid. A profile
   that is long gamma everywhere in range has no flip, and inventing one is
   worse than reporting its absence.

Roughly 50x the arithmetic of the naive method and still trivially fast on a
cached chain.

### Other measured outputs

- `net_gex` — total signed gamma notional, in dollars per 1% move.
- `call_wall` — strike with the largest positive gamma notional.
- `put_wall` — strike with the largest negative gamma notional.
- `profile` — the per-strike rows: strike, OI, gamma, gamma notional, sign,
  `src`, confidence, `clamped` flag.
- `spot`, and each level's distance from spot in points and percent.

No verdict field. No bias field.

### Expiry scope

0DTE plus the next two expiries plus the front monthly OPEX, deduplicated.
Longer-dated open interest contributes little gamma and a lot of fetch latency.
Configurable via `GEX_EXPIRIES`.

---

## Component 3: `core/market_data.py` — full-chain fetch

One new helper:

```python
full_chain(symbol, expiries) -> list[dict]
```

Reuses the existing `_option_chain` per-pass cache and its TTL/eviction, so a
GEX request during a flow scan costs no extra fetches. Existing call sites slice
near-the-money; GEX needs the entire strike range. SPX runs ~1,500 rows per
expiry, comfortably within budget for a cached fetch.

Returns rows normalized to the keys `gex.py` consumes, so the aggregation layer
never touches a DataFrame or a yfinance shape.

---

## Component 4: `/api/gex`

```
GET /api/gex?symbol=SPX
```

Follows the existing endpoint conventions in `web/app.py`, including whatever
auth/rate-limit decorators the neighbouring routes carry (`tests/test_security.py`
covers this and must keep passing).

Response carries the profile, `net_gex`, `flip`, `flips`, `call_wall`,
`put_wall`, `spot`, and `provenance`.

### Provenance

Modelled directly on `get_flow_source()`. That function exists because
TastyTrade can be fully configured and still fail every login, and the served
flow must not be labelled live when it is 15-minute-delayed yfinance. The same
discipline applies here for the same reason:

```
provenance = {
  oi_source:        "yfinance",
  oi_asof:          "prior session settle",   # OI is NOT intraday
  oi_stale:         true,
  expiries:         [...],
  strikes_total:    int,
  strikes_dropped:  int,   # no usable IV from feed or solver
  strikes_clamped:  int,   # near-expiry gamma ceiling applied
  inferred_pct:     float, # share of |gamma notional| with an observed sign
  assumed_pct:      float,
  ts:               float,
  age_s:            float,
}
```

`oi_stale` is `true` whenever open interest comes from settled data, which on
the yfinance path is always. It is a field rather than a constant so a future
live-OI feed can set it honestly without a UI change.

---

## Component 5: GEX tab

Seventh tab, after UOA. Hand-rolled SVG, matching the existing style in
`web/static/app.js` — no chart library is used anywhere in this project.

- Horizontal bars by strike: puts left, calls right, length by gamma notional.
- Horizontal rule at spot; second rule at the flip (dashed, and omitted entirely
  when there is no flip in range).
- Call and put walls labelled with strike and dollar notional.
- Per-strike bars visually distinguish `inferred` from `assumed` sign, so the
  observed portion of the surface is readable at a glance.
- Header: net GEX in dollars per 1% move, flip level, distance from spot. Values
  and units only — no verdict.
- Provenance strip along the bottom, always visible, stating OI staleness and
  the inferred/assumed split. Not collapsible.

---

## Component 6: Daily brief

A `GAMMA` line inside the `KEY LEVELS` block of the briefing format defined in
`CLAUDE.md`:

```
KEY LEVELS
  Resistance:  XXXX.XX / XXXX.XX
  Support:     XXXX.XX / XXXX.XX
  Gamma:       flip XXXX.XX | call wall XXXX.XX | put wall XXXX.XX | net $X.XXB/1%
```

Levels and magnitudes only. The `BIAS` and `PLAY TYPE` lines of the briefing are
authored as they are today and are not driven by this data.

---

## Component 7: Constants

New entries in `core/constants.py`, grouped and commented alongside the existing
tunables:

| Constant | Default | Purpose |
|---|---|---|
| `GEX_EXPIRIES` | `3` | Near expiries included, plus front monthly OPEX |
| `GEX_GAMMA_CLAMP_X` | `20.0` | Per-strike gamma notional ceiling, as a multiple of chain median |
| `GEX_INFER_MIN_CONTRACTS` | `250` | Absolute classified-volume floor for sign inference |
| `GEX_INFER_MIN_SHARE` | `0.60` | Classified share of strike volume required to infer |
| `GEX_GRID_PCT` | `0.05` | Flip search spans ±5% around spot |
| `GEX_GRID_STEPS` | `201` | Grid resolution before bisection |
| `GEX_FLIP_TOLERANCE` | `0.25` | Bisection precision, index points |

Defaults are starting points, not calibrated values, and are documented as such.

---

## Testing

TDD — tests written before implementation.

### `tests/test_greeks.py`

- `bs_greeks` against published Black-Scholes reference values.
- Put-call parity: call delta − put delta = 1 at the same strike.
- Gamma and vega identical for a call and a put at the same strike.
- `T → 0`: ATM gamma grows without converging; the one-minute floor bounds it.
- `implied_vol` round-trip: price a contract at a known sigma, recover it.
- `implied_vol` returns `None` below intrinsic and on non-positive prices.
- Bisection fallback engages where Newton's vega underflows (deep OTM).
- `bs_delta` and `norm_cdf` produce identical values after the move to
  `greeks.py` — a regression guard on the refactor.

### `tests/test_gex.py`

- Synthetic chain with hand-computed net gamma notional.
- Flip solver lands on an analytically-known root of a constructed profile.
- Grid method and naive strike-interpolation disagree on a chain built so they
  must — proving the grid method is actually running.
- Multiple roots are all reported; `flip` is the one nearest spot.
- `flip` is `None`, not fabricated, when the profile never crosses zero.
- Sign inference flips correctly on ask-heavy versus bid-heavy flow.
- Both inference gates independently reject: high share with low contract count,
  and high count with a split share.
- Strikes with no usable IV are excluded and counted, not silently zeroed.
- Clamping engages on a 0DTE ATM strike and increments the count.
- `inferred_pct` and `assumed_pct` are weighted by gamma notional, not by
  strike count.

### `tests/test_api_gex.py`

- Response shape and required provenance keys.
- `oi_stale` is `true` on the yfinance path.
- No forecast-shaped key (`bias`, `verdict`, `regime`, `target`, `signal`,
  `score`) appears anywhere in the response. This encodes the prime directive
  as an executable assertion rather than a comment.
- Endpoint respects the same auth/rate-limit posture as its neighbours.

---

## Build order

1. `core/greeks.py` + `tests/test_greeks.py`; refactor `core/options.py` to
   import and re-export. Full existing suite must stay green.
2. `core/constants.py` additions.
3. `core/market_data.py` → `full_chain`.
4. `core/gex.py` + `tests/test_gex.py`.
5. `/api/gex` + `tests/test_api_gex.py`.
6. GEX tab.
7. Daily-brief `GAMMA` line.

Steps 1–5 are shippable and verifiable without any UI.

## Known limitations

Recorded here so they are not rediscovered as bugs:

- Open interest is prior-session settled on the yfinance path. The profile is
  least accurate on days with heavy overnight repositioning.
- Sign inference only sees flow the scanner captured this session. Early in the
  session nearly the whole surface is `assumed`.
- Dealer hedging is an inference about behaviour, not an observation. Inventory
  does not obligate a price path.
- Index gamma ignores hedging that occurs in ES futures or correlated products.
- Clamping trades a small bias for stability on 0DTE. The count is surfaced so
  the trade-off stays visible.
