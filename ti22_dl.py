#!/usr/bin/env python3
"""Thin shim (Phase 1 package move): use `ti22-dl` or
`python -m titanium_downloader` instead. Kept so existing
`python ti22_dl.py ...` invocations keep working.
"""

import sys

from titanium_downloader.cli import main

if __name__ == "__main__":
  sys.exit(main())
