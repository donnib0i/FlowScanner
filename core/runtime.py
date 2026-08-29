"""
core/runtime.py -- Process-wide setup every scanner module depends on.

Importing any core module must be enough to silence pandas/yfinance warning
noise and initialize colorama, however the module was reached (CLI, web app,
or a test importing one file in isolation).
"""
import os
import sys
import warnings

# Absolute `core.` / `data.` imports work when scanner.py is run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

warnings.filterwarnings("ignore")

try:
    import colorama
    colorama.init(autoreset=True)
except Exception:  # colorama is optional at import time
    pass
