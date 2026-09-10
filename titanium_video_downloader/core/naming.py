"""Portable output naming (Windows-reserved set, strictest OS)."""

import re
from pathlib import Path

WINDOWS_RESERVED_NAMES = {
  "CON", "PRN", "AUX", "NUL",
  *(f"COM{i}" for i in range(1, 10)),
  *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize(name):
  """Portable filename stem: strips the Windows-reserved set (strictest OS),
  which also covers macOS/Linux restrictions. Replacement is always '_'."""
  name = re.sub(r"\s+", " ", name).strip()
  name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
  if name.upper() in WINDOWS_RESERVED_NAMES:
    name = "_" + name
  return name or "video"


def sanitize_path(p):
  """Sanitize the filename stem of a user-supplied path, keep dir + suffix."""
  p = Path(p)
  return p.parent / (sanitize(p.stem) + p.suffix)
