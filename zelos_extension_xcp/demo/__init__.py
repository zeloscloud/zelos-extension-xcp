"""Demo ECU: a measurement-only XCP-on-Ethernet slave with its A2L.

Backs the `demo` transport and the integration tests.
"""

from zelos_extension_xcp.demo.ecu import DemoEcu

__all__ = ["DemoEcu"]
