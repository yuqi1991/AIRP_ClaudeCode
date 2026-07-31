"""Compatibility import surface for the retired direct executor protocol.

New Studio Graph runs use :mod:`airp.engine.graph_runtime` and
:mod:`airp.engine.node_runner`. The implementation remains under ``compat``
only for existing embedders and old card/test migration paths.
"""

from airp.compat.legacy_director import *  # noqa: F401,F403
