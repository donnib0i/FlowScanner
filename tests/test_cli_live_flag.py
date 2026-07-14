from core.scanner import build_parser


def test_parser_has_live_and_interval():
    p = build_parser()
    ns = p.parse_args(["--live", "--interval", "30"])
    assert ns.live is True
    assert ns.interval == 30


def test_live_defaults_off():
    ns = build_parser().parse_args([])
    assert ns.live is False
