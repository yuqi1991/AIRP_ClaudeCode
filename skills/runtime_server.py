"""Compatibility module alias for :mod:`airp.server`.

Using the same module object preserves monkeypatching and module-level
configuration for legacy tests and extensions during the migration.
"""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("airp.server")
