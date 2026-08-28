"""
Calibrated probabilistic scoring for scanner candidates.

WHY THIS EXISTS
    `trade_grade` buckets `setup_q * 50 + opt_score * 0.30 + 20` into A/B/C/D.
    Those weights were asserted, never measured, and a letter carries no
    probability — so there is no principled way to rank two A's against each
    other, size against confidence, or check whether an A actually beats a B.

    The honest replacement is a probability with a measured reliability and an
    expected value. This module fits that probability on backtest history,
    validates it on data it never saw, and — this is the important part —
    REFUSES to release a model that does not earn its keep out of sample.

    A refusal is a real result, not a failure. A number that claims 70% and
    delivers 40% is worse than no number, because the trader sizes against it.

WHAT IT IS NOT
    No deep learning, no ensembles, no impressive names. Ridge-penalised
    logistic regression on the component scores the scanner already computes,
    fitted with Newton/IRLS in ~40 lines, with every coefficient printable.
    If a linear model on eight features cannot beat a constant, nothing here
    can, and the extra machinery would only hide that.

GUARDS AGAINST LOOK-AHEAD
    1. FEATURES is an explicit allowlist of fields knowable at signal time.
       OUTCOME_FIELDS is the matching denylist; `assert_no_lookahead` fails
       loudly if the two ever overlap, and a test pins it.
    2. Splits are by DATE, never by row. Rows from one session are ~15x
       correlated (see `effective_sample_size`), so a random split puts a
       trade's own session in the training set and inflates every metric.
    3. An embargo of EMBARGO_DAYS sessions is dropped between train and test.
       Forward returns look one bar ahead, so the last training day's outcome
       is realised inside the test window without it.
"""
from __future__ import annotations

import glob
import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

# ─── Feature contract ────────────────────────────────────────────────────────
# Everything here is known at the close of the signal bar. Nothing else may be.
SIGNAL_TYPES = (
    "gap_up", "gap_down", "unfilled_gap", "inside", "double_inside",
    "trend", "highvol", "a_plus", "breakout", "vwap_reclaim",
)

FEATURES: Tuple[str, ...] = (
    "setup_q",       # the existing composite, kept so we can measure it directly
    "log_rel_vol",   # log so a 10x volume day isn't 10x the weight of a 1x day
    "abs_gap",
    "hv20",
    "ema_bull",
    "above_vwap",
    "aplus_score",
    "dir_up",
) + tuple(f"is_{s}" for s in SIGNAL_TYPES)

# Fields that only exist after the trade resolved. Feeding any of these to the
# model would produce a beautiful, useless backtest.
OUTCOME_FIELDS: frozenset = frozenset({
    "fwd_return_pct", "entry_return_pct", "signed_return_pct",
    "direction_correct", "next_open", "next_close", "intraday_range_pct",
    "opt_pnl_pct", "opt_pnl_usd", "opt_entry", "opt_exit",
})


def assert_no_lookahead() -> None:
    """Fail loudly if a feature name ever collides with an outcome field."""
    overlap = set(FEATURES) & OUTCOME_FIELDS
    if overlap:
        raise ValueError(f"look-ahead: features leak outcome fields {sorted(overlap)}")


# ─── Sufficiency thresholds ──────────────────────────────────────────────────
# To claim "predicted 70% hits ~70%" you must be able to tell 70% from 60%.
# That is a +-5pp confidence interval on a bucket: n = 1.96^2 * 0.25 / 0.05^2.
MIN_BUCKET_N = 385
N_RELIABILITY_BUCKETS = 5
MIN_EFFECTIVE_N = MIN_BUCKET_N * N_RELIABILITY_BUCKETS   # 1925
MIN_DISTINCT_DATES = 250        # ~1 trading year; below this a regime IS the sample
EMBARGO_DAYS = 2
TRAIN_FRACTION = 0.70

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "results", "calibration_model.json")


# ─── Loading history ─────────────────────────────────────────────────────────
def _signal_type(rec: Dict) -> Optional[str]:
    """Older runs wrote signal_types[]; newer ones write signal. Accept both."""
    if rec.get("signal"):
        return rec["signal"]
    types = rec.get("signal_types") or []
    return types[0] if types else None


def record_key(rec: Dict) -> Tuple:
    """Identity of an observation. Runs overlap heavily — the same 60-day
    window re-scanned seven times is not seven independent samples, and
    counting it that way is the single easiest way to fake significance."""
    return (rec.get("date"), rec.get("ticker"), _signal_type(rec))


def load_records(results_dir: Optional[str] = None) -> List[Dict]:
    """Load every backtest run, deduplicated by (date, ticker, signal), sorted
    by date. Malformed files are skipped rather than aborting the load — a
    truncated run should not deny the user their sample count."""
    if results_dir is None:
        results_dir = os.path.dirname(MODEL_PATH)
    seen: Dict[Tuple, Dict] = {}
    for path in sorted(glob.glob(os.path.join(results_dir, "*.json"))):
        if os.path.basename(path) == os.path.basename(MODEL_PATH):
            continue
        try:
            with open(path) as fh:
                blob = json.load(fh)
        except (ValueError, OSError):
            continue
        recs = blob.get("records") if isinstance(blob, dict) else blob
        if not isinstance(recs, list):
            continue
        for rec in recs:
            if isinstance(rec, dict) and rec.get("date"):
                seen.setdefault(record_key(rec), rec)
    return sorted(seen.values(), key=lambda r: r["date"])


# ─── Feature extraction ──────────────────────────────────────────────────────
def _f(rec: Dict, key: str, default: float = 0.0) -> float:
    val = rec.get(key, default)
    try:
        out = float(val)
        return default if out != out else out       # NaN != NaN
    except (TypeError, ValueError):
        return default


def feature_vector(rec: Dict) -> List[float]:
    """Map one record (backtest row or live scanner result) onto FEATURES.
    Live rows and backtest rows share field names, so one extractor serves
    both — which is the only way the fitted weights mean anything at scan time."""
    stype = _signal_type(rec) or rec.get("signal_combo")
    vec = [
        _f(rec, "setup_q"),
        math.log10(max(_f(rec, "rel_vol", 1.0), 0.01)),
        abs(_f(rec, "gap_pct")),
        _f(rec, "hv20"),
        1.0 if rec.get("ema_bull") else 0.0,
        1.0 if rec.get("above_vwap") else 0.0,
        _f(rec, "aplus_score"),
        1.0 if rec.get("direction") == "up" else 0.0,
    ]
    vec += [1.0 if stype == s else 0.0 for s in SIGNAL_TYPES]
    return vec


def label_direction(rec: Dict) -> Optional[int]:
    val = rec.get("direction_correct")
    return None if val is None else int(val)


def label_option_win(rec: Dict) -> Optional[int]:
    """Did the simulated option position finish green? This is the label that
    matters for EV — being right on direction and still losing to theta and
    the spread is the normal outcome, not the exception."""
    pnl = rec.get("opt_pnl_pct")
    return None if pnl is None else int(pnl > 0)


LABELS = {"direction": label_direction, "option_win": label_option_win}


def build_dataset(records: Sequence[Dict], label: str = "option_win"):
    """Returns (X, y, dates). Rows without a resolved label are dropped."""
    if label not in LABELS:
        raise ValueError(f"unknown label {label!r}; choose from {sorted(LABELS)}")
    fn = LABELS[label]
    X, y, dates = [], [], []
    for rec in records:
        lab = fn(rec)
        if lab is None:
            continue
        X.append(feature_vector(rec))
        y.append(lab)
        dates.append(rec["date"])
    return (np.asarray(X, dtype=float).reshape(len(X), -1),
            np.asarray(y, dtype=float),
            np.asarray(dates, dtype=object))


# ─── Effective sample size ───────────────────────────────────────────────────
def effective_sample_size(y: np.ndarray, dates: np.ndarray) -> Dict:
    """
    Raw row count lies. On any given session most names move together, so 100
    rows from one day carry nowhere near 100 days' worth of information.

    We measure that with the intra-cluster correlation of the outcome across
    sessions and divide the row count by the resulting design effect
    (Kish: deff = 1 + (mbar - 1) * icc). The result is the number the
    sufficiency gate is allowed to reason about.
    """
    n = len(y)
    uniq = sorted(set(dates.tolist()))
    k = len(uniq)
    if n == 0 or k == 0:
        return {"n_rows": 0, "n_dates": 0, "icc": 0.0, "design_effect": 1.0,
                "effective_n": 0.0, "mean_rows_per_date": 0.0}
    if k < 2:
        return {"n_rows": n, "n_dates": k, "icc": 1.0, "design_effect": float(n),
                "effective_n": 1.0, "mean_rows_per_date": float(n)}

    mbar = n / k
    p = float(y.mean())
    within = p * (1.0 - p)
    if within <= 0 or mbar <= 1:
        # A degenerate outcome (all wins or all losses) carries no information
        # about clustering; treat every row as its own cluster rather than
        # inventing an ICC from a zero variance.
        return {"n_rows": n, "n_dates": k, "icc": 0.0, "design_effect": 1.0,
                "effective_n": float(n), "mean_rows_per_date": mbar}

    between = 0.0
    for d in uniq:
        mask = dates == d
        between += mask.sum() * (float(y[mask].mean()) - p) ** 2
    between /= (k - 1)

    icc = max(0.0, (between - within) / (within * (mbar - 1)))
    deff = 1.0 + (mbar - 1.0) * icc
    return {"n_rows": n, "n_dates": k, "icc": round(icc, 4),
            "design_effect": round(deff, 2),
            "effective_n": round(n / deff, 1),
            "mean_rows_per_date": round(mbar, 1)}


# ─── Splitting ───────────────────────────────────────────────────────────────
def purged_time_split(dates: np.ndarray, train_fraction: float = TRAIN_FRACTION,
                      embargo_days: int = EMBARGO_DAYS):
    """
    Split by session, oldest-first, with `embargo_days` sessions dropped from
    the end of train. Forward returns reach one bar past the signal, so without
    the embargo the final training rows resolve inside the test window and the
    model is scored partly on outcomes it was fitted on.
    """
    uniq = sorted(set(dates.tolist()))
    cut = int(len(uniq) * train_fraction)
    train_dates = set(uniq[:max(0, cut - embargo_days)])
    test_dates = set(uniq[cut:])
    train = np.array([d in train_dates for d in dates], dtype=bool)
    test = np.array([d in test_dates for d in dates], dtype=bool)
    return train, test


# ─── Model ───────────────────────────────────────────────────────────────────
@dataclass
class LogisticModel:
    """Ridge-penalised logistic regression, fitted by Newton/IRLS.

    Deliberately hand-rolled: it is 20 lines, adds no dependency, and every
    coefficient is a printable log-odds contribution the user can argue with.
    The intercept is never penalised — shrinking it would bias the base rate,
    which is the one quantity we most need to be right."""
    weights: List[float] = field(default_factory=list)
    feature_names: Tuple[str, ...] = FEATURES

    @staticmethod
    def _design(X: np.ndarray) -> np.ndarray:
        return np.hstack([np.ones((len(X), 1)), X])

    def fit(self, X: np.ndarray, y: np.ndarray, l2: float = 1.0,
            max_iter: int = 200, tol: float = 1e-8) -> "LogisticModel":
        Xb = self._design(X)
        w = np.zeros(Xb.shape[1])
        penalty = np.r_[0.0, np.ones(Xb.shape[1] - 1)]   # intercept unpenalised
        for _ in range(max_iter):
            p = 1.0 / (1.0 + np.exp(-Xb @ w))
            W = np.clip(p * (1.0 - p), 1e-6, None)
            grad = Xb.T @ (y - p) - l2 * penalty * w
            hess = (Xb.T * W) @ Xb + l2 * np.diag(penalty)
            try:
                step = np.linalg.solve(hess, grad)
            except np.linalg.LinAlgError:
                break
            w = w + step
            if np.max(np.abs(step)) < tol:
                break
        self.weights = [float(v) for v in w]
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        w = np.asarray(self.weights, dtype=float)
        return 1.0 / (1.0 + np.exp(-(self._design(np.asarray(X, dtype=float)) @ w)))

    def predict_one(self, rec: Dict) -> float:
        return float(self.predict_proba(np.array([feature_vector(rec)]))[0])

    def coefficients(self) -> List[Tuple[str, float]]:
        """(name, log-odds weight), largest magnitude first. Intercept included."""
        pairs = [("intercept", self.weights[0])]
        pairs += list(zip(self.feature_names, self.weights[1:]))
        return sorted(pairs, key=lambda t: -abs(t[1]))

    def to_dict(self) -> Dict:
        return {"weights": self.weights, "feature_names": list(self.feature_names)}

    @classmethod
    def from_dict(cls, blob: Dict) -> "LogisticModel":
        return cls(weights=[float(v) for v in blob["weights"]],
                   feature_names=tuple(blob.get("feature_names", FEATURES)))


# ─── Scoring metrics ─────────────────────────────────────────────────────────
def brier_score(y: np.ndarray, p: np.ndarray) -> float:
    """Mean squared error of the probability. Lower is better; a constant
    predictor at the base rate scores base*(1-base)."""
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(np.asarray(p, dtype=float), 1e-9, 1 - 1e-9)
    y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def auc_score(y: np.ndarray, p: np.ndarray) -> float:
    """Rank AUC via the Mann-Whitney statistic. 0.5 = coin flip; below 0.5
    means the ranking is actively inverted, which is worth knowing."""
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    n1 = float(y.sum())
    n0 = float(len(y) - n1)
    if n1 == 0 or n0 == 0:
        return 0.5
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p), dtype=float)
    sp = p[order]
    i = 0
    while i < len(sp):                                # average ties
        j = i
        while j + 1 < len(sp) and sp[j + 1] == sp[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def reliability_table(y: np.ndarray, p: np.ndarray,
                      n_buckets: int = N_RELIABILITY_BUCKETS) -> List[Dict]:
    """
    Bucketed predicted-vs-actual — the reliability curve as a table.

    Buckets are equal-count quantiles of the prediction, not equal-width bins:
    with predictions clumped in a narrow band, equal-width bins leave four
    empty rows and one row holding everything, which reads as calibration but
    measures nothing. Each row carries a Wald 95% band so the user can see
    whether a gap between predicted and actual is real or just thin.
    """
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    if len(y) == 0:
        return []
    edges = np.quantile(p, np.linspace(0, 1, n_buckets + 1))
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, n_buckets - 1)
    rows: List[Dict] = []
    for b in range(n_buckets):
        mask = idx == b
        n = int(mask.sum())
        if n == 0:
            continue
        actual = float(y[mask].mean())
        se = math.sqrt(max(actual * (1 - actual), 1e-12) / n)
        rows.append({
            "bucket": b,
            "n": n,
            "predicted": round(float(p[mask].mean()), 4),
            "actual": round(actual, 4),
            "ci95": round(1.96 * se, 4),
            "calibrated": abs(float(p[mask].mean()) - actual) <= 1.96 * se,
        })
    return rows


def expected_calibration_error(rows: Sequence[Dict]) -> float:
    """Sample-weighted mean |predicted - actual| across reliability buckets."""
    total = sum(r["n"] for r in rows)
    if not total:
        return 0.0
    return round(sum(r["n"] * abs(r["predicted"] - r["actual"]) for r in rows) / total, 4)


# ─── Expected value ──────────────────────────────────────────────────────────
def expected_value(prob: float, win_payoff_pct: float, loss_payoff_pct: float) -> float:
    """
    EV per dollar risked, as a fraction.

    Payoffs are signed percentages of the option premium as the backtest
    simulates them: `target_pct` on the win side, `-stop_pct` on the loss side,
    both already net of the simulated spread. Passing raw price targets here
    instead of option payoffs is the classic way to make a losing strategy look
    profitable — a 0.5% underlying move is not a 0.5% option move.
    """
    win = win_payoff_pct / 100.0
    loss = loss_payoff_pct / 100.0
    return round(prob * win + (1.0 - prob) * loss, 6)


def realised_payoffs(records: Sequence[Dict]) -> Optional[Tuple[float, float]]:
    """Mean win % and mean loss % of the simulated option P&L in `records`.
    Uses what the backtest actually realised rather than the configured
    target/stop, because stops slip and targets are rarely reached exactly."""
    wins = [r["opt_pnl_pct"] for r in records
            if r.get("opt_pnl_pct") is not None and r["opt_pnl_pct"] > 0]
    losses = [r["opt_pnl_pct"] for r in records
              if r.get("opt_pnl_pct") is not None and r["opt_pnl_pct"] <= 0]
    if not wins or not losses:
        return None
    return (float(np.mean(wins)), float(np.mean(losses)))


# ─── The gate ────────────────────────────────────────────────────────────────
@dataclass
class CalibrationReport:
    """Everything needed to decide whether to trust the probability — and the
    reason, in words, when the answer is no."""
    label: str
    released: bool
    reasons: List[str]
    sample: Dict
    train_n: int = 0
    test_n: int = 0
    train_dates: int = 0
    test_dates: int = 0
    base_rate_train: float = 0.0
    base_rate_test: float = 0.0
    brier: float = 0.0
    brier_baseline: float = 0.0
    log_loss: float = 0.0
    log_loss_baseline: float = 0.0
    auc: float = 0.5
    ece: float = 0.0
    reliability: List[Dict] = field(default_factory=list)
    coefficients: List[Tuple[str, float]] = field(default_factory=list)
    model: Optional[LogisticModel] = None
    payoffs: Optional[Tuple[float, float]] = None

    @property
    def samples_needed(self) -> int:
        """How many more effective samples before the gate could even open."""
        return int(max(0, MIN_EFFECTIVE_N - self.sample.get("effective_n", 0)))

    @property
    def dates_needed(self) -> int:
        return int(max(0, MIN_DISTINCT_DATES - self.sample.get("n_dates", 0)))

    def to_dict(self) -> Dict:
        blob = {k: v for k, v in self.__dict__.items() if k != "model"}
        blob["coefficients"] = [list(c) for c in self.coefficients]
        blob["samples_needed"] = self.samples_needed
        blob["dates_needed"] = self.dates_needed
        if self.model is not None:
            blob["model"] = self.model.to_dict()
        return blob


def evaluate(records: Sequence[Dict], label: str = "option_win",
             train_fraction: float = TRAIN_FRACTION,
             embargo_days: int = EMBARGO_DAYS,
             l2: float = 1.0) -> CalibrationReport:
    """
    Fit on the oldest sessions, score on the newest, and decide whether the
    result is honest enough to ship.

    Three gates, all of which must pass:
      1. Enough EFFECTIVE samples and enough distinct sessions.
      2. Held-out Brier better than predicting the training base rate for
         everything. A model that cannot beat a constant is a constant with
         extra steps and a false air of precision.
      3. Held-out AUC above 0.5 — the ranking must at least point the right way,
         since ranking by EV is the entire point.
    """
    assert_no_lookahead()
    X, y, dates = build_dataset(records, label=label)
    sample = effective_sample_size(y, dates)
    reasons: List[str] = []

    if sample["effective_n"] < MIN_EFFECTIVE_N:
        reasons.append(
            f"effective sample {sample['effective_n']:.0f} < {MIN_EFFECTIVE_N} required "
            f"({sample['n_rows']} rows / design effect {sample['design_effect']} from "
            f"intra-session correlation {sample['icc']})")
    if sample["n_dates"] < MIN_DISTINCT_DATES:
        reasons.append(
            f"{sample['n_dates']} distinct sessions < {MIN_DISTINCT_DATES} required "
            f"(one market regime is not a sample of market regimes)")

    train, test = purged_time_split(dates, train_fraction, embargo_days)
    report = CalibrationReport(
        label=label, released=False, reasons=reasons, sample=sample,
        train_n=int(train.sum()), test_n=int(test.sum()),
        train_dates=len(set(dates[train].tolist())),
        test_dates=len(set(dates[test].tolist())),
        payoffs=realised_payoffs(records),
    )
    if train.sum() < 30 or test.sum() < 30:
        report.reasons.append("not enough rows on both sides of the time split to validate")
        return report

    model = LogisticModel().fit(X[train], y[train], l2=l2)
    p_test = model.predict_proba(X[test])
    base = float(y[train].mean())
    const = np.full(int(test.sum()), base)

    report.model = model
    report.coefficients = model.coefficients()
    report.base_rate_train = round(base, 4)
    report.base_rate_test = round(float(y[test].mean()), 4)
    report.brier = round(brier_score(y[test], p_test), 4)
    report.brier_baseline = round(brier_score(y[test], const), 4)
    report.log_loss = round(log_loss(y[test], p_test), 4)
    report.log_loss_baseline = round(log_loss(y[test], const), 4)
    report.auc = round(auc_score(y[test], p_test), 4)
    report.reliability = reliability_table(y[test], p_test)
    report.ece = expected_calibration_error(report.reliability)

    if report.brier >= report.brier_baseline:
        report.reasons.append(
            f"held-out Brier {report.brier} is no better than predicting the base rate "
            f"({report.brier_baseline}) — the fit carries no out-of-sample information")
    if report.auc <= 0.5:
        report.reasons.append(
            f"held-out AUC {report.auc} <= 0.50 — the ranking does not beat chance")

    report.released = not report.reasons
    return report


def walk_forward(records: Sequence[Dict], label: str = "option_win",
                 fractions: Sequence[float] = (0.5, 0.6, 0.7, 0.8, 0.9),
                 embargo_days: int = EMBARGO_DAYS, l2: float = 1.0) -> List[Dict]:
    """
    Expanding-window folds, always training on the past and testing on the
    immediate future. A single train/test split can be lucky; if the model only
    beats the constant in some folds, it does not beat it.
    """
    assert_no_lookahead()
    X, y, dates = build_dataset(records, label=label)
    uniq = sorted(set(dates.tolist()))
    if len(uniq) < 6:
        return []
    window = max(1, int(len(uniq) * 0.10))
    folds: List[Dict] = []
    for frac in fractions:
        cut = int(len(uniq) * frac)
        tr_d = set(uniq[:max(0, cut - embargo_days)])
        te_d = set(uniq[cut:cut + window])
        tr = np.array([d in tr_d for d in dates], dtype=bool)
        te = np.array([d in te_d for d in dates], dtype=bool)
        if tr.sum() < 50 or te.sum() < 20:
            continue
        model = LogisticModel().fit(X[tr], y[tr], l2=l2)
        p = model.predict_proba(X[te])
        const = np.full(int(te.sum()), float(y[tr].mean()))
        folds.append({
            "fraction": frac,
            "train_n": int(tr.sum()), "test_n": int(te.sum()),
            "brier": round(brier_score(y[te], p), 4),
            "brier_baseline": round(brier_score(y[te], const), 4),
            "auc": round(auc_score(y[te], p), 4),
            "beat_baseline": brier_score(y[te], p) < brier_score(y[te], const),
        })
    return folds


# ─── Release artifact ────────────────────────────────────────────────────────
def save_model(report: CalibrationReport, path: str = MODEL_PATH) -> Optional[str]:
    """Persist ONLY a released model. Writing a rejected one to disk is how a
    fitted-on-noise model quietly becomes the default six months later."""
    if not report.released or report.model is None:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(report.to_dict(), fh, indent=2)
    return path


def load_model(path: str = MODEL_PATH) -> Optional[LogisticModel]:
    """Return the released model, or None. None is the normal state — callers
    must handle it, and must not substitute a guess."""
    try:
        with open(path) as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return None
    if not blob.get("released") or "model" not in blob:
        return None
    try:
        return LogisticModel.from_dict(blob["model"])
    except (KeyError, TypeError, ValueError):
        return None


# ─── Scanner integration ─────────────────────────────────────────────────────
UNCALIBRATED = "uncalibrated"
CALIBRATED = "calibrated"


def annotate(results: List[Dict], model: Optional[LogisticModel] = None,
             payoffs: Optional[Tuple[float, float]] = None) -> List[Dict]:
    """
    Attach `win_prob`, `expected_value` and `calibration` to each scanner
    result, in place, and return the list ranked by EV.

    With no released model this attaches `calibration: "uncalibrated"`,
    leaves win_prob and expected_value as None, and returns the input order
    untouched — the existing setup_q ordering stays in charge. Silence beats a
    made-up 0.5.
    """
    if model is None or payoffs is None:
        for r in results:
            r["win_prob"] = None
            r["expected_value"] = None
            r["calibration"] = UNCALIBRATED
        return results

    win_pct, loss_pct = payoffs
    for r in results:
        prob = model.predict_one(r)
        r["win_prob"] = round(prob, 4)
        r["expected_value"] = expected_value(prob, win_pct, loss_pct)
        r["calibration"] = CALIBRATED
    return sorted(results, key=lambda r: r["expected_value"], reverse=True)


def calibration_status(path: str = MODEL_PATH) -> Dict:
    """One-line status for the scanner header, so an uncalibrated run says so
    on screen instead of silently looking the same as a calibrated one."""
    model = load_model(path)
    if model is None:
        return {"calibrated": False,
                "note": "no calibrated model released — ranking by setup quality"}
    try:
        with open(path) as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        blob = {}
    return {
        "calibrated": True,
        "label": blob.get("label"),
        "effective_n": blob.get("sample", {}).get("effective_n"),
        "held_out_brier": blob.get("brier"),
        "held_out_auc": blob.get("auc"),
        "note": "probabilities validated on held-out sessions",
    }


# ─── Report rendering ────────────────────────────────────────────────────────
def format_report(report: CalibrationReport, folds: Optional[Sequence[Dict]] = None) -> str:
    """Plain text, no colour — this gets pasted into notes and diffed."""
    s = report.sample
    out = [
        f"CALIBRATION REPORT — label: {report.label}",
        "=" * 68,
        "",
        "SAMPLE",
        f"  rows (deduplicated)      {s.get('n_rows', 0)}",
        f"  distinct sessions        {s.get('n_dates', 0)}   (need {MIN_DISTINCT_DATES})",
        f"  rows per session         {s.get('mean_rows_per_date', 0)}",
        f"  intra-session corr (ICC) {s.get('icc', 0)}",
        f"  design effect            {s.get('design_effect', 1)}x",
        f"  EFFECTIVE sample         {s.get('effective_n', 0)}   (need {MIN_EFFECTIVE_N})",
        "",
        "SPLIT (oldest sessions train, newest test, "
        f"{EMBARGO_DAYS}-session embargo between)",
        f"  train  {report.train_n} rows / {report.train_dates} sessions"
        f"   base rate {report.base_rate_train}",
        f"  test   {report.test_n} rows / {report.test_dates} sessions"
        f"   base rate {report.base_rate_test}",
        "",
        "HELD-OUT PERFORMANCE (never fitted on)",
        f"  Brier      {report.brier}   vs constant baseline {report.brier_baseline}",
        f"  log loss   {report.log_loss}   vs constant baseline {report.log_loss_baseline}",
        f"  AUC        {report.auc}   (0.50 = coin flip)",
        f"  ECE        {report.ece}",
        "",
        "RELIABILITY — predicted vs actual, held out",
    ]
    if report.reliability:
        out.append("  bucket    n   predicted   actual   95% band   ok")
        for row in report.reliability:
            out.append("  {b:>6} {n:>4}     {p:.3f}    {a:.3f}    +-{c:.3f}   {ok}".format(
                b=row["bucket"], n=row["n"], p=row["predicted"], a=row["actual"],
                c=row["ci95"], ok="yes" if row["calibrated"] else "NO"))
    else:
        out.append("  (no held-out rows)")

    if folds:
        out += ["", "WALK-FORWARD (expanding window)",
                "  train_n  test_n   brier   baseline   auc   beat?"]
        for f in folds:
            out.append("  {tn:>7} {sn:>7}   {b:.4f}   {bb:.4f}   {a:.3f}   {ok}".format(
                tn=f["train_n"], sn=f["test_n"], b=f["brier"],
                bb=f["brier_baseline"], a=f["auc"],
                ok="yes" if f["beat_baseline"] else "no"))
        beat = sum(1 for f in folds if f["beat_baseline"])
        out.append(f"  folds beating the constant baseline: {beat}/{len(folds)}")

    if report.coefficients:
        out += ["", "COEFFICIENTS (log-odds, train fold)"]
        for name, w in report.coefficients[:10]:
            out.append(f"  {name:<16} {w:+.4f}")

    out += ["", "VERDICT"]
    if report.released:
        out.append("  RELEASED — probabilities are validated out of sample.")
    else:
        out.append("  NOT RELEASED. Existing setup_q / letter-grade scoring stays the default.")
        for reason in report.reasons:
            out.append(f"    - {reason}")
        if report.samples_needed:
            out.append(f"  need ~{report.samples_needed} more effective samples")
        if report.dates_needed:
            out.append(f"  need ~{report.dates_needed} more trading sessions of backtest history")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fit and validate calibrated scoring.")
    ap.add_argument("--label", default="option_win", choices=sorted(LABELS))
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--save", action="store_true",
                    help="persist the model if (and only if) it passes the gate")
    args = ap.parse_args(argv)

    records = load_records(args.results_dir)
    if not records:
        print("No backtest records found. Run the backtest first.")
        return 1
    report = evaluate(records, label=args.label)
    print(format_report(report, walk_forward(records, label=args.label)))
    if args.save:
        path = save_model(report)
        print(f"\nsaved: {path}" if path else "\nnot saved (gate not passed)")
    return 0 if report.released else 2


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
