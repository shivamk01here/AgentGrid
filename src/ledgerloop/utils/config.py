"""Configuration utilities."""

from __future__ import annotations

import os
from pathlib import Path


def load_config(path: str | Path = ".env", apply: bool = True) -> dict[str, str]:
    """Load a simple .env file into a dict.

    Supports KEY=VALUE lines. Lines starting with # are comments.
    Existing environment variables are never overridden.

    Args:
        path: Path to the .env file.
        apply: If True, set keys missing from os.environ.

    Returns:
        Dict of key-value pairs loaded from the file.
    """
    config: dict[str, str] = {}
    env_path = Path(path)

    if not env_path.exists():
        return config

    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            config[key] = _parse_value(value)

    if apply:
        # The file is not allowed to win over a variable somebody exported
        # before we were started, but reading the value back is still
        # correct: `os.environ` keeps the exported one, and the caller gets
        # to see what the file asked for.
        for key, value in config.items():
            if key not in os.environ:
                os.environ[key] = value

    return config


def _parse_value(raw: str) -> str:
    """Read the value half of a KEY=VALUE line.

    A quoted value is taken exactly as written between its quotes, so a
    password or URL with " #" in it survives; whatever trails the closing
    quote is a comment and is dropped. An unquoted value ends at the first
    " #", and a stray quote with no partner is trimmed off as it always was.
    """
    raw = raw.strip()
    if raw[:1] in {'"', "'"}:
        closing = raw.find(raw[0], 1)
        if closing != -1:
            return raw[1:closing]
    if " #" in raw:
        raw = raw.split(" #", 1)[0]
    return raw.strip().strip('"').strip("'")
