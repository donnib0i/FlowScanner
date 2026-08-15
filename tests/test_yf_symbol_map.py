from core.scanner import _yf_ticker


def test_spx_maps_to_option_bearing_symbol():
    # ^GSPC serves price history but exposes ZERO option expiries, so every
    # SPX contract lookup (find, ladder, flow) 404'd. ^SPX carries both.
    assert _yf_ticker("SPX") == "^SPX"


def test_index_symbols_are_prefixed():
    assert _yf_ticker("VIX") == "^VIX"
    assert _yf_ticker("RUT") == "^RUT"
    assert _yf_ticker("NDX") == "^NDX"


def test_equities_pass_through_untouched():
    assert _yf_ticker("NVDA") == "NVDA"
    assert _yf_ticker("BRK-B") == "BRK-B"


def test_lookup_is_case_insensitive():
    assert _yf_ticker("spx") == "^SPX"
