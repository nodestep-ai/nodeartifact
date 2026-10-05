from importlib.metadata import version

from nodeartifact.configuration import configure
from nodeartifact.instrumentation import (
    InstrumentedGraph,
    TracingMiddleware,
    instrument,
)

__version__ = version("nodeartifact")

__all__ = [
    "InstrumentedGraph",
    "TracingMiddleware",
    "__version__",
    "configure",
    "instrument",
]
