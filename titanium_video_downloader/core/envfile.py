"""Minimal .env loader (stdlib only): flat KEY=VALUE secrets.

No interpolation, no multiline values, no `export` handling — by design
(credential files should stay boring). If quoting/multiline needs ever
appear, swap this module for python-dotenv; the call surface stays the same.
"""

import os
from pathlib import Path

APP_VENDOR = "titanium-software"
APP_NAME = "ti22-video-dl"
CONFIG_DIR_ENV = "TI22_VIDEO_DL_CONFIG_DIR"


def default_config_dir():
  """Per-user config root; explicit TI22_CONFIG_DIR always wins."""
  override = os.environ.get(CONFIG_DIR_ENV)
  if override:
    return Path(override)
  if os.name == "nt":
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_VENDOR / APP_NAME
  return Path.home() / ".config" / APP_VENDOR / APP_NAME


def find_dotenv():
  """First hit wins: ./.env, then <config-dir>/.env. Returns Path or None."""
  for candidate in (Path.cwd() / ".env", default_config_dir() / ".env"):
    try:
      if candidate.is_file():
        return candidate
    except OSError:
      continue
  return None


def parse_dotenv_text(text):
  """Parse KEY=VALUE pairs. Returns (pairs dict, bad-line list, names only)."""
  pairs = {}
  bad = []
  for lineno, line in enumerate(str(text or "").splitlines(), 1):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
      continue
    if "=" not in stripped:
      bad.append(lineno)
      continue
    key, _, value = stripped.partition("=")
    key = key.strip()
    if not key.replace("_", "").isalnum() or key[0].isdigit():
      bad.append(lineno)
      continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
      value = value[1:-1]
    pairs[key] = value
  return pairs, bad


def load_dotenv(path=None):
  """Load .env into os.environ via setdefault (real env always wins).

  Returns the Path loaded, or None. Always names the loaded file (path
  only, never values): with two lookup locations (./.env, then the app
  config dir), silent first-hit-wins otherwise lets you edit the wrong
  file with full confidence. Warnings name lines, never values.
  """
  import sys
  target = Path(path) if path else find_dotenv()
  if target is None:
    return None
  try:
    pairs, bad = parse_dotenv_text(target.read_text(encoding="utf-8"))
  except OSError as e:
    print(f"note: could not read .env at {target}: {e}", file=sys.stderr)
    return None
  for key, value in pairs.items():
    os.environ.setdefault(key, value)
  print(f"note: loaded .env from {target}", file=sys.stderr)
  if bad:
    lines = ",".join(str(n) for n in bad)
    print(f"note: ignoring {len(bad)} unparsable .env line(s) "
          f"({target}, lines {lines})", file=sys.stderr)
  return target
