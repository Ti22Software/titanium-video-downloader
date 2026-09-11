"""User config file (config.toml) + layered defaults resolution.

Precedence: CLI flag > environment (existing bindings) > config.toml >
built-ins. Secrets never live here — email/password stay env-only.

config.toml lives in the app config dir (see envfile.default_config_dir),
parsable by stdlib tomllib (no new dependency). Unknown keys, bad types,
and malformed files never fail a run: a stderr note names the problem
(never values) and the key/file is ignored.
"""

import sys

from .envfile import default_config_dir

CONFIG_FILENAME = "config.toml"

_BOOL_KEYS = (
  "keep_intermediate",
  "no_space_check",
  "debug_login",
  "headed",
  "no_auth",
  "pick_always",
)

_STR_KEYS = (
  "quality",
  "on_missing_quality",
  "ffmpeg_path",
  "output_dir",
  "temp_dir",
)

BUILTINS = {
  "quality": "best",
  "on_missing_quality": "fallback",
  "concurrency": 4,
  "ffmpeg_path": None,
  "output_dir": None,
  "temp_dir": None,
  "keep_intermediate": False,
  "no_space_check": False,
  "debug_login": False,
  "headed": False,
  "no_auth": False,
  "pick_always": False,
}

_ON_MISSING_CHOICES = ("fallback", "fail", "ask")


def default_config_path():
  return default_config_dir() / CONFIG_FILENAME


def load_config_file(path=None):
  """Read config.toml → (pairs dict, path or None).

  Missing file is not an error ({}). Malformed TOML or a non-table
  document notes to stderr and yields {}.
  """
  target = path if path is not None else default_config_path()
  try:
    with open(target, "rb") as f:
      import tomllib
      doc = tomllib.load(f)
  except FileNotFoundError:
    return {}, None
  except OSError as e:
    print(f"note: could not read config file at {target}: {e}",
          file=sys.stderr)
    return {}, None
  except Exception as e:
    print(f"note: ignoring malformed config file at {target}: {e}",
          file=sys.stderr)
    return {}, None
  if not isinstance(doc, dict):
    print(f"note: ignoring config file at {target}: not a TOML table",
          file=sys.stderr)
    return {}, None
  return doc, target


def _coerce(pairs, source_name):
  """Validated config values → (clean dict, {key: source})."""
  clean, sources = {}, {}
  for key in list(pairs):
    if (key not in _BOOL_KEYS and key not in _STR_KEYS
            and key != "concurrency"):
      print(f"note: ignoring unknown config key '{key}' ({source_name})",
            file=sys.stderr)
      continue
    value = pairs[key]
    if key in _BOOL_KEYS:
      if not isinstance(value, bool):
        print(f"note: ignoring config key '{key}': expected true/false "
              f"({source_name})", file=sys.stderr)
        continue
    elif key == "concurrency":
      if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        print(f"note: ignoring config key 'concurrency': expected a "
              f"positive integer ({source_name})", file=sys.stderr)
        continue
    else:
      if not isinstance(value, str):
        print(f"note: ignoring config key '{key}': expected a string "
              f"({source_name})", file=sys.stderr)
        continue
      if not value.strip():
        print(f"note: ignoring config key '{key}': empty value "
              f"({source_name})", file=sys.stderr)
        continue
      if key == "on_missing_quality" and value not in _ON_MISSING_CHOICES:
        print(f"note: ignoring config key 'on_missing_quality': expected "
              f"one of {', '.join(_ON_MISSING_CHOICES)} ({source_name})",
              file=sys.stderr)
        continue
    clean[key] = value
    sources[key] = source_name
  return clean, sources


def resolve_config(args, config_path=None, file_pairs=None):
  """Layer CLI > config.toml > built-ins onto args (in place + returned).

  Flags argparse leaves as None (user did not pass them) fall through to
  the config file, then built-ins. Returns (args, sources dict) where
  sources maps key → 'cli' | 'config' | 'builtin:<default>'.
  """
  if file_pairs is None:
    file_pairs, _found = load_config_file(config_path)
  source_name = (f"config file {config_path}" if config_path
                 else f"config file {default_config_path()}")
  clean, file_sources = _coerce(file_pairs, source_name)
  sources = {}
  for key, builtin in BUILTINS.items():
    cli_value = getattr(args, key, None)
    if cli_value is not None:
      sources[key] = "cli"
      continue
    if key in clean:
      setattr(args, key, clean[key])
      sources[key] = "config"
    else:
      setattr(args, key, builtin)
      sources[key] = f"builtin:{builtin}"
  # --pick / --no-pick / pick_always tri-state (config-only flag, CLI wins).
  pick_always_src = sources.get("pick_always", "builtin:False")
  if getattr(args, "pick", None):
    args.pick = True
    sources["pick"] = "cli:--pick"
  elif getattr(args, "no_pick", None):
    args.pick = False
    sources["pick"] = "cli:--no-pick"
  else:
    args.pick = bool(getattr(args, "pick_always", False))
    sources["pick"] = (f"config:pick_always={args.pick_always}"
                       if pick_always_src == "config" else "builtin:False")
  return args, sources


def effective_config(args, sources, config_path):
  """JSON-serializable snapshot for --print-config (no secrets)."""
  keys = list(BUILTINS) + ["pick"]
  return {
    "config_file": str(config_path) if config_path else None,
    "values": {k: getattr(args, k, None) for k in keys},
    "sources": {k: sources.get(k, "?") for k in keys},
  }
