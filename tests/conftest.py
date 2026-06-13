import os
import sys

# Put the scanner repo root on sys.path so `import data...` / `import core...` work,
# matching the absolute-import style already used in core/universe.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
