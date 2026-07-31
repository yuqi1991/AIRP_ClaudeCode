"""Compatibility import surface for the RP Host Session Turn Runtime.

The durable Session/Revision/Card projection runtime is an RP host concern;
the content-neutral Graph Runtime lives in :mod:`airp.engine.graph_runtime`.
"""

from airp.host.rp.session_runtime import *  # noqa: F401,F403
