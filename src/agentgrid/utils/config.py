"""Configuration utilities."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def load_config(path: str | Path = ".env") -> dict[str, str]:
    """Load a simple .env file into a dict.

    Supports KEY=VALUE lines. Lines starting with # are comments.
    Does NOT override existing environment variables.

    Args:
        path: Path to the .env file.

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

    return config
