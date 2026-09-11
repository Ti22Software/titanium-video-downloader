"""Batch downloads: file parsing, dedupe, per-entry control flow.

Batch = text file and/or CLI URLs combined into one ordered run. Sequential
per video (pipeline parallelism is a fast-follow); one shared session and
one shared browser (see core/browser.py) across entries.
"""

from pathlib import Path

from ..extractors.base import fail


class EntrySkip(Exception):
  """One batch entry skipped (exists / user-skipped); batch continues."""


def parse_batch_file(path):
  """Parse "URL [QUALITY]" lines: blanks/#-comments ignored.

  Returns [(url, quality|None)]. Malformed lines fail fast naming the line.
  """
  try:
    text = Path(path).read_text(encoding="utf-8")
  except OSError as e:
    fail(f"cannot read batch file '{path}': {e}")
  entries = []
  for lineno, line in enumerate(text.splitlines(), 1):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
      continue
    parts = stripped.split(None, 1)
    url = parts[0]
    quality = parts[1].strip() if len(parts) > 1 else None
    if not url:
      fail(f"batch file '{path}' line {lineno}: empty URL.")
    entries.append((url, quality or None))
  if not entries:
    fail(f"batch file '{path}' has no usable entries.")
  return entries


def dedupe_entries(entries):
  """Collapse exact (url, effective-quality) dupes, first wins.

  Returns (unique, skipped_notes). Effective quality normalizes None to
  "best" so `url` and `url best` dedupe; same URL at different qualities
  stays twice (different outputs — legitimate).
  """
  seen = set()
  unique = []
  notes = []
  for url, quality in entries:
    key = (url, quality or "best")
    if key in seen:
      notes.append(f"note: duplicate batch entry skipped: {url}")
      continue
    seen.add(key)
    unique.append((url, quality))
  return unique, notes


def is_url(text):
  """Heuristic splitting CLI positionals: URLs start with http(s)."""
  low = (text or "").lower()
  return low.startswith("http://") or low.startswith("https://")
