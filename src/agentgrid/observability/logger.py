"""Structured logging for AgentGrid components."""

from __future__ import annotations

import logging
import sys
from typing import Any


def get_logger(name: str, **bindings: Any) -> logging.Logger:
    """Get a configured logger for an AgentGrid component.

    Args:
        name: Logger name (typically module path).
        **bindings: Extra context fields attached to every log line.

    Returns:
        A configured stdlib logger.
    """
    logger = logging.getLogger(f"agentgrid.{name}")

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)

    if bindings:
        extra = logging.LoggerAdapter(logger, bindings)
        return extra.logger  # type: ignore[return-value]

    return logger
