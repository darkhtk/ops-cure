from __future__ import annotations

import os
import re
from collections.abc import Mapping

UTF8_ENV_DEFAULTS: dict[str, str] = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}

POWERSHELL_UTF8_PROLOGUE = (
    "[Console]::InputEncoding=[System.Text.UTF8Encoding]::UTF8; "
    "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::UTF8; "
    "$OutputEncoding=[System.Text.UTF8Encoding]::UTF8; "
    "chcp 65001 > $null; "
)

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
ACTIVITY_LINE_LIMIT = 280


def build_utf8_subprocess_env(
    base: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = dict(base or os.environ)
    for key, value in UTF8_ENV_DEFAULTS.items():
        env[key] = env.get(key) or value
    env["NO_COLOR"] = "1"
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def text_subprocess_kwargs() -> dict[str, object]:
    return {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }


def decode_text_output(payload: bytes | str | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    return payload.decode("utf-8", errors="replace")


def wrap_powershell_utf8(script: str) -> str:
    return POWERSHELL_UTF8_PROLOGUE + script


def normalize_activity_line(payload: str | None) -> str | None:
    if not payload:
        return None
    text = ANSI_ESCAPE_RE.sub("", payload)
    text = text.replace("\r", "\n")
    candidates = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    candidates = [line for line in candidates if line]
    if not candidates:
        return None
    latest = candidates[-1]
    if len(latest) > ACTIVITY_LINE_LIMIT:
        latest = latest[: ACTIVITY_LINE_LIMIT - 12].rstrip() + " [truncated]"
    return latest
