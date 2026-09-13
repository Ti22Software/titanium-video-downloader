"""Titanium Downloader: multi-site streaming downloader (CLI first)."""

try:
    from ._version import __version__
except ImportError:  # source checkout, never built (dev runs, tests)
    __version__ = "unknown"
