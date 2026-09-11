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


def _render(profile, spot, flip=None, call_wall=None, put_wall=None, width=403):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed — chart layout unverified")
    d = {"spot": spot, "profile": profile, "flip": flip,
         "call_wall": call_wall, "put_wall": put_wall}
    src = (_fn("gexAxisMoney") + "\n" + _fn("gexWindow") + "\n" +
           _fn("renderGexChart") + "\n" +
           f"process.stdout.write(renderGexChart({json.dumps(d)},{width}));")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


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
