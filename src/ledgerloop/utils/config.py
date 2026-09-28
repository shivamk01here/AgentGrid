"""Configuration utilities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


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
            value = value.strip()
            if " #" in value:
                value = value.split(" #", 1)[0]
            value = value.strip('"').strip("'")
            config[key] = value

    if apply:
        # The file is not allowed to win over a variable somebody exported
        # before we were started, but reading the value back is still
        # correct: `os.environ` keeps the exported one, and the caller gets
        # to see what the file asked for.
        for key, value in config.items():
            if key not in os.environ:
                os.environ[key] = value

    return config
