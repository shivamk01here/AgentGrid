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
        return cls(
            log_level=os.getenv("AGENTGRID_LOG_LEVEL", "INFO"),
            env=os.getenv("AGENTGRID_ENV", "development"),
            tracing_enabled=os.getenv("AGENTGRID_TRACING_ENABLED", "true").lower() == "true",
            metrics_enabled=os.getenv("AGENTGRID_METRICS_ENABLED", "true").lower() == "true",
            auth_enabled=os.getenv("AGENTGRID_AUTH_ENABLED", "false").lower() == "true",
            api_key=os.getenv("AGENTGRID_API_KEY", ""),
        )

    @property
    def is_production(self) -> bool:
        return self.env == "production"
