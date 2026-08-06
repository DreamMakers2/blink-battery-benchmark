"""Logging configuration with conservative secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path


class SecretRedactionFilter(logging.Filter):
    """Remove common credential and authorization values from rendered messages."""

    PATTERNS = (
        re.compile(
            r"(?i)(password|access[_ -]?token|refresh[_ -]?token|authorization|token|2fa|mfa|pin)(\s*[:=]\s*)([^\s,;]+)"
        ),
        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern in self.PATTERNS:
            if pattern.groups >= 3:
                message = pattern.sub(r"\1\2[REDACTED]", message)
            else:
                message = pattern.sub(r"\1[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(log_file: Path, verbose: bool = False) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redaction = SecretRedactionFilter()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redaction)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
