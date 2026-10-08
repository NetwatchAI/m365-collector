"""Multi-tenant Microsoft 365 collector exposing Prometheus metrics."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("m365c")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"
