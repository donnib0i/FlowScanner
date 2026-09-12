"""
Two schemes, one token system.

The monochrome redesign routed every directional rule through --up/--dn and
said in its own header that restoring two colours was a matter of setting those
two tokens. That held -- this is that change, plus a switch and a default.

The default is the two-colour scheme. Reading a flow feed means telling a call
from a put at a glance, and luminance alone makes that a comparison instead of
a recognition. Monochrome stays one tap away.
"""
import re

CSS = open("web/static/app.css").read()
JS = open("web/static/app.js").read()
HTML = open("web/templates/index.html").read()


def _block(selector):
    i = CSS.index(selector)
    return CSS[i:CSS.index("}", i)]


# ── the scheme exists and actually differs ───────────────────────────────────
def test_the_colour_scheme_overrides_the_two_tokens_that_encode_direction():
    b = _block(':root[data-scheme="color"]')
    for tok in ("--up:", "--dn:"):
        m = re.search(re.escape(tok) + r"\s*(#[0-9a-fA-F]{3,8})", b)
        assert m, f"{tok} is not given a hue in the colour scheme"
    assert re.search(r"--up:\s*#fff", b, re.I) is None, "up is still white"


def test_up_and_down_are_not_the_same_colour():
    b = _block(':root[data-scheme="color"]')
    up = re.search(r"--up:\s*(#[0-9a-fA-F]+)", b)[1].lower()
    dn = re.search(r"--dn:\s*(#[0-9a-fA-F]+)", b)[1].lower()
    assert up != dn


def test_the_magnitude_ramp_stays_neutral_in_both_schemes():
    # --m1..--m5 encode how much, never which way. Colouring them would put hue
    # back on the axis the redesign deliberately cleared.
    b = _block(':root[data-scheme="color"]')
    assert not re.search(r"--m[1-5]\s*:", b), "the magnitude ramp was recoloured"


def test_the_three_chart_levels_are_named_for_meaning_not_colour():
    # So the scheme decides whether they separate by hue or luminance alone.
    for tok in ("--lvl-spot", "--lvl-flip", "--lvl-fut"):
        assert tok in CSS, f"{tok} missing"
        assert f"var({tok})" in JS, f"the chart does not use {tok}"


def test_the_chart_levels_separate_in_the_colour_scheme():
    b = _block(':root[data-scheme="color"]')
    vals = {t: re.search(re.escape(t) + r":\s*([^;]+);", b)
            for t in ("--lvl-spot", "--lvl-flip", "--lvl-fut")}
    assert all(vals.values()), "the levels are not re-pointed in the colour scheme"
    resolved = {k: v[1].strip() for k, v in vals.items()}
    assert len(set(resolved.values())) == 3, f"levels collide: {resolved}"


# ── the switch ───────────────────────────────────────────────────────────────
def test_the_default_is_the_two_colour_scheme():
    assert "'color'" in JS.split("function toggleScheme")[0] or \
           '"color"' in JS.split("function toggleScheme")[0]
    boot = HTML[HTML.index("<head>"):HTML.index("</head>")]
    assert "scanner_scheme" in boot
    assert re.search(r"var v\s*=\s*'color'", boot), "the boot default is not colour"


def test_the_scheme_is_applied_before_first_paint():
    # Left to the main script the page renders one scheme then swaps on every
    # load. The boot script has to come before the stylesheet does its work.
    head = HTML[HTML.index("<head>"):HTML.index("</head>")]
    assert head.index("scanner_scheme") < head.index("{{APP_CSS}}"), \
        "the scheme is applied after the stylesheet -- the page will flash"


def test_the_choice_survives_a_reload():
    assert "localStorage.setItem('scanner_scheme'" in JS


def test_the_toggle_is_reachable_from_every_tab():
    # It sits in the top bar, which is outside the tab panes.
    assert 'id="scheme-btn"' in HTML
    head = HTML[:HTML.index('id="content"')]
    assert 'id="scheme-btn"' in head, "the toggle is inside a tab pane"


def test_storage_being_unavailable_does_not_break_the_page():
    # Private windows and blocked site data throw on access.
    boot = HTML[HTML.index("<head>"):HTML.index("</head>")]
    assert "try{" in boot and "catch" in boot
    m = re.search(r"function _applyScheme\(v\)\{(.*?)\n\}", JS, re.S)
    assert m and "try{" in m[1] and "catch" in m[1]
