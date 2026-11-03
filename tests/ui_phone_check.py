"""Superseded by tests/ui_redesign_check.py (the front end was redesigned on 3 Nov 2026). Kept so old commands still work."""
import os
import runpy
import sys

sys.argv = [sys.argv[0]] + sys.argv[1:]
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_redesign_check.py"), run_name="__main__")
