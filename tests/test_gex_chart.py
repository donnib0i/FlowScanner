"""
The GEX chart's layout, actually executed.

Three things went wrong here at once and none of them failed loudly. A full SPX
chain drew every strike from 5000 to 9000, which is a 12,000px ladder that is
~80% blank rows and too heavy for the browser to rasterise. The rule captions
were positioned at a fixed offset from the plot, so "SPOT 7599.64" ran past the
viewBox and lost its last characters. And the SVG was a hard-coded 340px inside
a 403px column, leaving a dead strip on every phone.

The renderer is sliced out of app.js and run under node against a synthetic
chain, so these assert what the browser will draw rather than that the source
mentions the fix.
"""
import json
import re
import shutil
import subprocess

import pytest

JS = open("web/static/app.js").read()


def _fn(name):
    """One top-level function, sliced out by its closing brace at column 0."""
    head = f"function {name}("
    assert head in JS, f"{name} is gone -- re-point this test"
    return head + JS.split(head)[1].split("\n}\n")[0] + "\n}"


def _render(profile, spot, flip=None, call_wall=None, put_wall=None, width=403,
            futures=None, unit=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed — chart layout unverified")
    d = {"spot": spot, "profile": profile, "flip": flip,
         "call_wall": call_wall, "put_wall": put_wall, "futures": futures,
         "symbol": "SPX"}
    u = unit or {"ratio": 1, "fut": False}
    src = (_fn("gexLevel") + "\n" + _fn("gexAxisMoney") + "\n" + _fn("gexWindow") +
           "\n" + _fn("renderGexChart") + "\n" +
           f"process.stdout.write(renderGexChart({json.dumps(d)},{width},{json.dumps(u)}));")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


# A measured ES link, shaped like core.futures.link_for returns.
ES = {"future": "ES", "yf_symbol": "ES=F", "name": "E-mini S&P 500",
      "ratio": 1.0010274, "ratio_asof": "2026-09-10", "basis": 7.80,
      "last": 7620.50, "prev_close": 7599.50, "change": 21.0,
      "change_pct": 0.00276, "implied_underlying": 7612.68,
      "contracts": [
          {"code": "ES", "yf": "ES=F", "name": "E-mini S&P 500",
           "multiplier": 50.0, "tick": 0.25, "micro": False,
           "last": 7620.50, "tick_value": 12.5},
          {"code": "MES", "yf": "MES=F", "name": "Micro E-mini S&P 500",
           "multiplier": 5.0, "tick": 0.25, "micro": True,
           "last": 7620.50, "tick_value": 1.25}]}
FUT_UNIT = {"ratio": ES["ratio"], "fut": True}


def _chain(lo, hi, step, spot, width):
    """A chain whose gamma clusters near spot, the way a real one does."""
    import math
    prof = []
    k = lo
    while k < hi:
        w = math.exp(-0.5 * ((k - spot) / width) ** 2)
        for t in ("call", "put"):
            mag = w * 1e9
            prof.append({"strike": round(k, 2), "type": t, "oi": int(w * 9000),
                         "src": "inferred",
                         "gamma_notional": mag if t == "call" else -mag * 1.3})
        k += step
    return prof


def _svg_attrs(svg):
    m = re.search(r'<svg width="(\d+)" height="(\d+)" viewBox="0 0 (\d+) (\d+)"', svg)
    assert m, "svg header changed shape -- re-point this test"
    return {"w": int(m[1]), "h": int(m[2]), "vbw": int(m[3]), "vbh": int(m[4])}


FULL = _chain(5000, 9000, 5, 7599.64, 90)


# ── the ladder is windowed to the strikes that carry the surface ─────────────
def test_a_full_index_chain_does_not_draw_every_strike():
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600)
    a = _svg_attrs(svg)
    assert a["h"] < 1200, f"ladder is {a['h']}px tall -- the window stopped working"
    assert svg.count("<rect") <= 70


def test_the_undrawn_strikes_are_declared_not_silently_dropped():
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600)
    assert "further strikes hold almost no gamma and are not drawn" in svg


def test_a_short_chain_is_drawn_whole_and_claims_nothing_was_dropped():
    svg = _render(_chain(15, 22.5, 2.5, 18.2, 3), 18.2, call_wall=20, put_wall=17.5)
    assert "not drawn" not in svg


def test_the_window_keeps_the_walls_it_names():
    # A wall far outside the dense band still has to appear: a chart that hides
    # the level printed in its own header is worse than a tall one.
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=8200, put_wall=7600)
    assert ">8200<" in svg, "the call wall was windowed out of its own chart"


# ── nothing is drawn outside the canvas ──────────────────────────────────────
def test_the_svg_fills_the_column_it_is_given():
    for width in (320, 403, 900):
        a = _svg_attrs(_render(FULL, 7599.64, flip=7597.17, call_wall=7700,
                               put_wall=7600, width=width))
        assert a["w"] == a["vbw"]
        assert abs(a["w"] - width) <= 4, f"{a['w']}px svg in a {width}px column"


def test_the_rule_captions_are_anchored_inside_the_right_edge():
    for width in (320, 403, 900):
        svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700,
                      put_wall=7600, width=width)
        vbw = _svg_attrs(svg)["vbw"]
        for x, cap in re.findall(r'<text x="([\d.]+)"[^>]*>((?:SPOT|FLIP)[^<]*)</text>', svg):
            assert float(x) <= vbw, f"{cap} starts past the {vbw}px viewBox"
        # end-anchored means the text grows leftward and cannot run off
        for m in re.finditer(r'<text x="[\d.]+"([^>]*)>(?:SPOT|FLIP)', svg):
            assert 'text-anchor="end"' in m[1]


# ── the strike axis reads like a price ladder ────────────────────────────────
def test_strike_labels_land_on_round_numbers():
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600)
    labels = [int(v) for v in re.findall(r'font-family="var\(--font\)">(\d{4})</text>', svg)]
    plain = [v for v in labels if v not in (7700, 7600)]   # walls are always labelled
    assert plain, "no strike labels at all"
    assert all(v % 25 == 0 for v in plain), f"odd strike labels: {sorted(set(plain))[:8]}"


def test_a_wide_ladder_does_not_lose_the_leading_digit_off_its_strikes():
    # QQQ strikes converted to NQ are eight characters ("30005.19"). The gutter
    # was fixed at 44px, which fits a four-digit SPX strike and cropped the
    # front off everything wider -- the axis read "0005.19".
    NQ = dict(ES, future="NQ", ratio=41.103, basis=None, last=29429.75,
              implied_underlying=715.99)
    qqq = _chain(600, 830, 1, 716.0, 9)
    svg = _render(qqq, 716.0, flip=715.20, call_wall=725, put_wall=710,
                  futures=NQ, unit={"ratio": 41.103, "fut": True})
    labels = re.findall(r'<text x="([\-\d.]+)" y="[\d.]+" text-anchor="end" '
                        r'font-size="8.5"[^>]*>([\d.]+)</text>', svg)
    assert labels, "no strike labels drawn"
    for x, lbl in labels:
        assert float(x) - len(lbl) * 5.1 >= -0.5, \
            f"{lbl} starts off-canvas at x={x} -- the gutter is too narrow"


def test_the_gutters_grow_with_the_numbers_that_go_in_them():
    narrow = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600)
    NQ = dict(ES, future="NQ", ratio=41.103, basis=None, last=29429.75,
              implied_underlying=715.99)
    wide = _render(_chain(600, 830, 1, 716.0, 9), 716.0, flip=715.20,
                   call_wall=725, put_wall=710, futures=NQ,
                   unit={"ratio": 41.103, "fut": True})

    def gutter(svg):
        return min(float(x) for x in re.findall(
            r'<text x="([\d.]+)" y="[\d.]+" text-anchor="end" font-size="8.5"', svg))

    assert gutter(wide) > gutter(narrow), "the gutter did not widen for wider labels"
    # The column itself is unchanged -- only the split inside it moves.
    assert _svg_attrs(narrow)["w"] == _svg_attrs(wide)["w"]


def test_every_strike_label_sits_in_the_same_gutter():
    # They used to flip to whichever side the bar pointed, which made the axis
    # zigzag down the page.
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600)
    anchors = re.findall(r'<text x="[\d.]+" y="[\d.]+" text-anchor="(\w+)" font-size="8.5"', svg)
    assert anchors and set(anchors) == {"end"}


# ── overlapping levels stay readable ─────────────────────────────────────────
def test_spot_and_a_flip_two_points_away_do_not_print_on_top_of_each_other():
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600)
    ys = [float(y) for y, _ in
          re.findall(r'<text x="[\d.]+" y="([\d.]+)"[^>]*>((?:SPOT|FLIP)[^<]*)</text>', svg)]
    assert len(ys) == 2
    assert abs(ys[0] - ys[1]) >= 9, f"captions are {abs(ys[0]-ys[1]):.1f}px apart"


def test_a_chain_with_no_flip_draws_only_the_spot_rule():
    svg = _render(FULL, 7599.64, flip=None, call_wall=7700, put_wall=7600)
    assert "SPOT " in svg and "FLIP " not in svg


# ── the bars have a scale to be read against ─────────────────────────────────
def test_the_magnitude_axis_is_labelled():
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600)
    assert "per 1% move" in svg
    assert re.search(r'>&#8722;\$[\d.]+[BMK]?</text>', svg), "no negative scale label"
    assert re.search(r'>\+\$[\d.]+[BMK]?</text>', svg), "no positive scale label"


def test_an_empty_profile_says_so_instead_of_drawing_an_empty_axis():
    assert "No strikes with usable data" in _render([], 7599.64)


# ── the surface read in futures prices ───────────────────────────────────────
def test_the_live_future_gets_its_own_rule_on_the_ladder():
    # Out of hours this is the only line on the chart still moving: the index
    # print is frozen at its close.
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600,
                  futures=ES)
    assert "ES 7620.50" in svg


def test_the_future_rule_is_placed_by_the_index_level_it_implies():
    # Plotting the raw futures print on a strike axis would put the line at the
    # wrong strike by the whole basis.
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600,
                  futures=ES)
    ys = {cap.split()[0]: float(y) for y, cap in
          re.findall(r'<line x1="[\d.]+" y1="([\d.]+)"[^>]*/><text[^>]*>((?:SPOT|FLIP|ES)[^<]*)', svg)}
    # implied_underlying 7612.68 sits above spot 7599.64, so a smaller y
    assert ys["ES"] < ys["SPOT"], "the ES rule ignored the basis"


def test_switching_to_futures_converts_every_level_on_the_chart():
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600,
                  futures=ES, unit=FUT_UNIT)
    assert ">7707.91<" in svg, "the 7700 call wall was not converted to ES"
    assert ">7700<" not in svg, "an unconverted index strike is still labelled"
    assert "SPOT 7607.45" in svg


def test_converted_strike_labels_keep_the_precision_the_basis_has():
    # 7700 index maps to 7707.91 in ES. Rounding that to 7708 would invent
    # precision the measured basis does not carry.
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600,
                  futures=ES, unit=FUT_UNIT)
    labels = re.findall(r'font-family="var\(--font\)">(\d+\.\d\d)</text>', svg)
    assert labels, "futures-unit labels lost their decimals"


def test_a_ticker_with_no_futures_counterpart_draws_no_future_rule():
    svg = _render(FULL, 7599.64, flip=7597.17, call_wall=7700, put_wall=7600,
                  futures=None)
    assert "SPOT " in svg and "FLIP " in svg
    assert " 7620.50" not in svg


# ── what reaching a level is worth ───────────────────────────────────────────
def _dist(call_wall=7700, flip=7597.17, put_wall=7600, contract=None):
    """renderGexDist, executed, for one selected contract size."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed — distance table unverified")
    d = {"futures": ES, "call_wall": call_wall, "flip": flip, "put_wall": put_wall}
    src = (_fn("gexDollars") + "\n" + _fn("gexSpec") + "\n"
           + "let _gexData=" + json.dumps(d) + ";\n"
           + "let _gexContract=" + json.dumps(contract) + ";\n"
           + _fn("gexContract") + "\n" + _fn("renderGexDist") + "\n"
           + "process.stdout.write(renderGexDist(_gexData));")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_distance_is_priced_on_the_selected_contract():
    # ES call wall: 7700 x 1.0010274 = 7707.91, which is 87.41 above the 7620.50
    # print. At $50/pt that is $4,371; the micro is a tenth of it.
    big = _dist(contract="ES")
    small = _dist(contract="MES")
    assert "$4,371" in big, big
    assert "$437" in small and "$4,371" not in small, small


def test_the_dollar_per_point_is_stated_next_to_the_numbers():
    assert "$50/pt" in _dist(contract="ES")
    assert "$5/pt" in _dist(contract="MES")


def test_a_sub_dollar_tick_keeps_its_cents():
    # A micro's tick is fifty cents; rounding it to the nearest dollar printed
    # MNQ's $0.50 tick as "$1" and overstated the smallest move it can make.
    assert "1 tick $1.25" in _dist(contract="MES")
    assert "1 tick $12.50" in _dist(contract="ES"), "ES ticks at $12.50, not $13"


def test_a_level_below_the_future_reads_as_a_negative_distance():
    out = _dist(contract="ES")
    # put wall 7600 -> 7607.81, below the 7620.50 print
    assert "&minus;12.69" in out, out
    assert "+87.41" in out, out


def test_levels_the_surface_could_not_measure_are_left_out():
    out = _dist(call_wall=None, flip=7597.17, put_wall=None, contract="ES")
    assert "CALL WALL" not in out and "PUT WALL" not in out
    assert "ZERO-GAMMA FLIP" in out


def test_no_measured_levels_means_no_table_rather_than_an_empty_one():
    assert _dist(call_wall=None, flip=None, put_wall=None, contract="ES") == ""


def test_the_default_size_is_the_full_contract():
    assert _dist(contract=None) == _dist(contract="ES")
