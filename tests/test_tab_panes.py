"""
Every tab pane must be a direct child of #content.

.tab-pane is display:none until it gets .active, so a pane nested inside
another pane can never be shown -- clicking its nav item lights up the icon
and renders a blank screen. That is exactly what happened to GEX: a missing
</div> after #uoa-table-wrap left #tab-gex inside #tab-uoa, and the tab was
dead on production for as long as it existed.
"""
from html.parser import HTMLParser

TABS = ["flow", "scan", "sectors", "find", "uoa", "gex", "intel"]


class _Panes(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.parent_of = {}

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        d = dict(attrs)
        self.stack.append(d.get("id"))
        if "tab-pane" in d.get("class", ""):
            self.parent_of[d.get("id")] = next(
                (x for x in reversed(self.stack[:-1]) if x), None)

    def handle_endtag(self, tag):
        if tag == "div" and self.stack:
            self.stack.pop()


def _panes():
    p = _Panes()
    p.feed(open("web/templates/index.html").read())
    return p.parent_of


def test_every_tab_pane_hangs_off_content():
    parent_of = _panes()
    for tab in TABS:
        pane = f"tab-{tab}"
        assert pane in parent_of, f"{pane} is not marked as a .tab-pane"
        assert parent_of[pane] == "content", (
            f"{pane} is nested inside #{parent_of[pane]} -- a hidden parent "
            f"means the {tab.upper()} tab renders blank")


def test_no_tab_pane_is_missing_from_the_template():
    assert set(_panes()) == {f"tab-{t}" for t in TABS}
