"""Browser-first remote Codex behavior scaffold.

This package is the landing zone for the canonical remote_codex behavior.
Its job is to own browser-truth work state on top of the generic kernel,
while browser and runtime surfaces stay in external adapters.
"""

from .service import RemoteCodexBehaviorService

__all__ = ["RemoteCodexBehaviorService"]
