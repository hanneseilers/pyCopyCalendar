"""Structured, privacy-conscious logging setup (spec section 18).

Every handler gets a redaction filter so a raw exception message or a
library repr can never leak the app password into stdout or the log
file, and a run-ID filter so every line - even ones logged before
reconcile.run() itself was called - can be correlated to one run in
cron's combined output.
"""

from __future__ import annotations

import contextvars
import logging
import logging.handlers
from pathlib import Path

from .config import LoggingConfig
from .credentials import Redactor

_run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")

_FORMAT = "%(asctime)s %(levelname)s run=%(run_id)s %(name)s %(message)s"


def set_current_run_id(run_id: str) -> None:
    _run_id_var.set(run_id)


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id_var.get()
        return True


class _RedactingFilter(logging.Filter):
    """Redacts secret values from both the raw message and any %-args,
    since either can carry an exception's text verbatim."""

    def __init__(self, redactor: Redactor):
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact(str(record.msg))
        if record.args:
            record.args = tuple(
                self._redactor.redact(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


def setup_logging(
    config: LoggingConfig, log_path: Path, redactor: Redactor, *, verbose: bool = False
) -> None:
    level = logging.DEBUG if verbose else getattr(logging, config.level, logging.INFO)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_FORMAT)
    run_id_filter = _RunIdFilter()
    redacting_filter = _RedactingFilter(redactor)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=config.max_bytes, backupCount=config.backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger("calendar_sync")
    root.setLevel(level)
    root.handlers.clear()
    for handler in (file_handler, stream_handler):
        handler.addFilter(run_id_filter)
        handler.addFilter(redacting_filter)
        root.addHandler(handler)
    root.propagate = False
