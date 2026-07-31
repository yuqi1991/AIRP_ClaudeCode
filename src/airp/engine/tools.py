"""Compatibility import surface for the RP Host tool registry.

The typed session/memory/worldbook tools are host capabilities, not Graph
Runtime primitives. New code should import them from ``airp.host.rp.tools``.
"""

from airp.host.rp.tools import *  # noqa: F401,F403
