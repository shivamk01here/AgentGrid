"""Configuration utilities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def load_config(path: str | Path = ".env", apply: bool = True) -> dict[str, str]:
    """Load a simple .env file into a dict.

    Supports KEY=VALUE lines. Lines starting with # are comments.
    Does NOT override existing environment variables unless apply=True.

    Args:
        path: Path to the .env file.
        apply: If True, set non-existing keys in os.environ.

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
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                config[key] = value

    if apply:
        for key, value in config.items():
            os.environ[key] = value

    return config
