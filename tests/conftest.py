import os
import sys

import pytest

# Put the scanner repo root on sys.path so `import data...` / `import core...` work,
# matching the absolute-import style already used in core/universe.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True, scope="session")
def _isolate_baseline_store(tmp_path_factory):
    """Keep the test run out of the developer's real observation history.

    Anything that exercises a scan now writes flow contracts to the baseline
    store. Left pointed at the default path a test run would add fake sessions
    to real history — and the repeat-hit tests, which assert exact day counts,
    would depend on how recently the scanner had been used.
    """
    os.environ["SCANNER_BASELINE_DB"] = str(
        tmp_path_factory.mktemp("baseline") / "baseline.db")
    yield
    os.environ.pop("SCANNER_BASELINE_DB", None)
