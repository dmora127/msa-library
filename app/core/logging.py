"""Logging with secret redaction.

A filter scrubs JWT-shaped strings and known credential key names from every log
record so tokens / API keys never land in logs (mirrors the client plan's
"never prints the API key" rule).
"""

from __future__ import annotations

import logging
import re

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|token|secret|password|signing[_-]?key)\b"
    r"(\s*[=:]\s*)(\S+)"
)


def _redact(text: str) -> str:
    text = _JWT_RE.sub("<redacted-jwt>", text)
    text = _KV_RE.sub(r"\1\2<redacted>", text)
    return text


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            record.args = tuple(
                _redact(a) if isinstance(a, str) else a for a in record.args
            )
        return True


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(RedactSecretsFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
