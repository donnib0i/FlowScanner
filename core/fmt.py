"""
core/fmt.py -- Terminal display helpers. Pure formatting -- colors, bars, and short
number/label renderings. No data fetching, no scoring.

Part of the scanner core; `core.scanner` re-exports everything here.
"""
from core import runtime as _runtime  # noqa: F401  (warnings/colorama setup)

from colorama import Fore, Style
from typing import Optional, Dict

from core.constants import _GRADE_COLORS


# ─── Display helpers ──────────────────────────────────────────────────────────
def fmt_vol(v: int) -> str:
    if v >= 1_000_000_000: return f"{v/1e9:.1f}B"
    if v >= 1_000_000:     return f"{v/1e6:.1f}M"
    if v >= 1_000:         return f"{v/1e3:.0f}K"
    return str(v)


def fmt_num(v: int) -> str:
    if v >= 1_000_000: return f"{v/1e6:.1f}M"
    if v >= 1_000:     return f"{v/1e3:.0f}k"
    return str(v)


def score_bar(score: int, width: int = 10) -> str:
    filled = int(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def score_colored(score: int) -> str:
    c = Fore.GREEN if score >= 75 else (Fore.YELLOW if score >= 50 else Fore.RED)
    return f"{c}{score_bar(score)}{Style.RESET_ALL} {c}{score:3d}{Style.RESET_ALL}"


def color_change(val: float) -> str:
    if val > 0: return Fore.GREEN + f"+{val:.2f}%" + Style.RESET_ALL
    if val < 0: return Fore.RED   + f"{val:.2f}%"  + Style.RESET_ALL
    return f"{val:.2f}%"


def color_gap(gap_pct: float, flag: Optional[str]) -> str:
    if flag == "gap_up":    return Fore.CYAN   + f"+{gap_pct:.2f}%^" + Style.RESET_ALL
    if flag == "gap_down":  return Fore.YELLOW + f"{gap_pct:.2f}%v"  + Style.RESET_ALL
    if abs(gap_pct) > 0.5:  return Fore.WHITE  + f"{gap_pct:+.2f}%✓" + Style.RESET_ALL
    return f"{gap_pct:+.2f}%"


def fmt_whale_score(score: int) -> str:
    bar = score_bar(score, width=8)
    if score >= 80:   c = Fore.RED + Style.BRIGHT
    elif score >= 60: c = Fore.YELLOW + Style.BRIGHT
    elif score >= 40: c = Fore.YELLOW
    else:             c = Fore.WHITE
    return f"{c}{bar} {score:3d}{Style.RESET_ALL}"


def grade_score(setup_q: float, opt_score: int, has_contract: bool) -> float:
    """The composite behind the letter. Same weighting the a_grade filter uses."""
    return setup_q * 50 + opt_score * 0.30 + (20 if has_contract else 0)


def grade_letter(setup_q: float, opt_score: int, has_contract: bool) -> str:
    """Plain A/B/C/D. The signal journal stores this — never the coloured form,
    because ANSI escapes in a database column make every later filter wrong."""
    score = grade_score(setup_q, opt_score, has_contract)
    if score >= 75: return "A"
    if score >= 55: return "B"
    if score >= 35: return "C"
    return "D"


def trade_grade(setup_q: float, opt_score: int, has_contract: bool) -> str:
    letter = grade_letter(setup_q, opt_score, has_contract)
    return _GRADE_COLORS[letter] + letter + Style.RESET_ALL


def level_str(level: Optional[Dict]) -> str:
    if not level:
        return "—"
    stars = "*" * min(4, level["strength"] // 2 + 1)
    ltype = "S" if level["type"] == "support" else "R"
    c     = Fore.GREEN if ltype == "S" else Fore.RED
    return f"{c}{ltype}${level['price']:.1f}{Style.RESET_ALL} {Fore.YELLOW}{stars}{Style.RESET_ALL}"


def fmt_flow(f: float) -> str:
    """Format dollar flow value."""
    if f >= 1_000_000: return f"${f/1e6:.1f}M"
    if f >= 1_000:     return f"${f/1e3:.0f}K"
    return f"${f:.0f}"


def fmt_bias_bar(call_flow: float, put_flow: float, width: int = 10) -> str:
    """Visual call/put split bar.  Cyan = calls, Yellow = puts."""
    total  = call_flow + put_flow
    if total <= 0:
        return Fore.WHITE + "─" * width + Style.RESET_ALL
    call_w = round(call_flow / total * width)
    put_w  = width - call_w
    return Fore.CYAN + "█" * call_w + Fore.YELLOW + "█" * put_w + Style.RESET_ALL


def fmt_voi(voi: float) -> str:
    """Color-coded vol/OI ratio string."""
    if voi >= 20:   c = Fore.RED   + Style.BRIGHT
    elif voi >= 10: c = Fore.RED
    elif voi >= 5:  c = Fore.YELLOW + Style.BRIGHT
    elif voi >= 2:  c = Fore.YELLOW
    else:           c = Fore.WHITE
    return f"{c}x{voi:.1f}{Style.RESET_ALL}"
