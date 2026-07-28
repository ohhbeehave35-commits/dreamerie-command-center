"""Make the app package importable from tests.

This deployment had no tests directory at all until now, so there was nothing
putting the repo root on sys.path. Adding it explicitly rather than relying on
pytest's rootdir inference keeps `pytest` working the same whether it is run
from the repo root or anywhere else.
"""

import pathlib
import sys

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
