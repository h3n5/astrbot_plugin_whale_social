"""Make the plugin root importable for the test-suite.

The plugin itself is loaded by AstrBot with the plugin directory on
``sys.path``; this conftest reproduces that so tests can import ``core`` and
``storage`` without an AstrBot installation.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
