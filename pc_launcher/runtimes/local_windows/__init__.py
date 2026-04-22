"""Local Windows runtime adapters."""

from .bridge import BridgeClient
from .launcher import LauncherDaemon
from .process_io import (
    build_utf8_subprocess_env,
    decode_text_output,
    normalize_activity_line,
    text_subprocess_kwargs,
    wrap_powershell_utf8,
)
from .verification import CommandVerificationRunner, VerificationResult
from .worker import WorkerRuntime

__all__ = [
    "BridgeClient",
    "CommandVerificationRunner",
    "LauncherDaemon",
    "VerificationResult",
    "WorkerRuntime",
    "build_utf8_subprocess_env",
    "decode_text_output",
    "normalize_activity_line",
    "text_subprocess_kwargs",
    "wrap_powershell_utf8",
]
