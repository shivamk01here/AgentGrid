"""Agent configuration helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    """Global runtime configuration loaded from environment."""

    log_level: str = "INFO"
    env: str = "development"
    tracing_enabled: bool = True
    metrics_enabled: bool = True
    auth_enabled: bool = False
    api_key: str = ""

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        """Load configuration from environment variables."""
        def _as_bool(value: str) -> bool:
            return value.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            log_level=os.getenv("LEDGERLOOP_LOG_LEVEL", "INFO"),
            env=os.getenv("LEDGERLOOP_ENV", "development"),
            tracing_enabled=_as_bool(os.getenv("LEDGERLOOP_TRACING_ENABLED", "true")),
            metrics_enabled=_as_bool(os.getenv("LEDGERLOOP_METRICS_ENABLED", "true")),
            auth_enabled=_as_bool(os.getenv("LEDGERLOOP_AUTH_ENABLED", "false")),
            api_key=os.getenv("LEDGERLOOP_API_KEY", ""),
        )

    @property
    def is_production(self) -> bool:
        return self.env == "production"
