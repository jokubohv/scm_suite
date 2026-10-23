"""Local path setup for the FS-00 platform contract tests.

The directory is named ``platform`` (task-49 scope) which collides with the Python stdlib module, so it
is intentionally *not* a package: helpers are imported as top-level modules after adding this directory
to ``sys.path`` for the duration of the test session.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
