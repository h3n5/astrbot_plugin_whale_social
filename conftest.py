"""Make the plugin root importable for the test-suite.

Tests import ``core`` and ``storage`` as top-level packages; those modules use
package-relative imports internally, so they work identically here and under
AstrBot, which loads the plugin as ``data.plugins.<name>`` (the plugin
directory itself is never put on ``sys.path``).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
