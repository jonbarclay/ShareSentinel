"""Structured JSON logging configuration for the worker service."""

import logging

from pythonjsonlogger.jsonlogger import JsonFormatter


def setup_logging(service_name: str, level: str = "INFO") -> None:
    """Configure structured JSON logging on the root logger.

    Args:
        service_name: Identifier added to every log record (e.g. "worker").
        level: Logging level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    root = logging.getLogger()

    # Avoid duplicate handlers on repeated calls
    if root.handlers:
        root.handlers.clear()

    handler = logging.StreamHandler()
    formatter = JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Inject service name into every log record via a custom factory
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        record = old_factory(*args, **kwargs)
        record.service = service_name  # type: ignore[attr-defined]
        return record

    logging.setLogRecordFactory(record_factory)
