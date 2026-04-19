from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.orm import Session

from .models import TranscriptModel

TOKEN_PATTERNS = [
    re.compile(r"(?i)(token|secret|password)\s*[:=]\s*([^\s]+)"),
    re.compile(r"\b[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{20,}\b"),
]


def sanitize_text(content: str) -> str:
    sanitized = content
    for pattern in TOKEN_PATTERNS:
        sanitized = pattern.sub(
            lambda match: f"{match.group(1) if match.lastindex else 'secret'}=[REDACTED]",
            sanitized,
        )
    return sanitized.strip()


class TranscriptService:
    def add_entry(
        self,
        db: Session,
        *,
        session_id: str,
        direction: str,
        actor: str,
        content: str,
        source_discord_message_id: Optional[str] = None,
    ) -> TranscriptModel:
        entry = TranscriptModel(
            session_id=session_id,
            direction=direction,
            actor=actor,
            content=sanitize_text(content),
            source_discord_message_id=source_discord_message_id,
        )
        db.add(entry)
        return entry

