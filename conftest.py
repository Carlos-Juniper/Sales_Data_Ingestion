"""
Root-level pytest conftest.

Adds ``connectors/`` to ``sys.path`` so that ``from lib import ...`` resolves
without requiring ``PYTHONPATH=connectors`` to be set manually.  This mirrors
exactly what each test suite's own conftest.py already does; centralising it
here means a bare ``pytest`` from the repo root works out of the box.

Packaging note: ``pyproject.toml`` declares ``connectors/`` as an editable
install (``pip install -e .``), so a bare ``python connectors/.../*.py``
invocation also resolves ``lib`` without a PYTHONPATH prefix.
"""

from __future__ import annotations

import os
import sys

# Ensure connectors/ is on the path once, before any test module imports.
_connectors_dir = os.path.join(os.path.dirname(__file__), "connectors")
if _connectors_dir not in sys.path:
    sys.path.insert(0, _connectors_dir)
