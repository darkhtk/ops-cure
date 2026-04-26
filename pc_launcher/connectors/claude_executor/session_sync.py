"""Scan ~/.claude/projects/ and produce session metadata for the bridge.

Mirrors claude-remote/src/sessions.js — read each <encoded-cwd>/<id>.jsonl,
extract title (first user message) + cwd, return a list ready to send
via BridgeClient.sync().
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable


HEAD_SCAN_BYTES = 64 * 1024
TITLE_MAX_LEN = 80


def default_projects_root() -> Path:
    return Path(os.path.expanduser("~/.claude/projects"))


def scan_sessions(projects_root: Path | None = None, *, limit: int = 500) -> list[dict[str, Any]]:
    root = projects_root or default_projects_root()
    if not root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            try: stat = jsonl.stat()
            except OSError: continue
            entries.append({
                "sessionId": jsonl.stem,
                "_jsonlPath": str(jsonl),
                "_dirEncoded": proj_dir.name,
                "_mtime": stat.st_mtime,
                "_size": stat.st_size,
            })
    entries.sort(key=lambda e: e["_mtime"], reverse=True)
    entries = entries[:limit]
    summarized = [_summarize(e) for e in entries]
    return summarized


def _summarize(entry: dict[str, Any]) -> dict[str, Any]:
    head = _read_head(entry["_jsonlPath"], HEAD_SCAN_BYTES)
    title, cwd, event_count, first_user = _parse_head(head)
    if not cwd:
        cwd = _decode_cwd_from_dir(entry["_dirEncoded"])
    return {
        "sessionId": entry["sessionId"],
        "cwd": cwd,
        "title": title or "(no preview)",
        "firstUserMessage": first_user,
        "updatedAtMs": int(entry["_mtime"] * 1000),
        "createdAtMs": int(entry["_mtime"] * 1000),
        "eventCount": event_count,
        "fileSize": entry["_size"],
        "jsonlPath": entry["_jsonlPath"],
    }


def _read_head(path: str, max_bytes: int) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_head(text: str) -> tuple[str, str, int, str]:
    title = ""
    cwd = ""
    event_count = 0
    first_user = ""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        event_count += 1
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        if not cwd and isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        if not title:
            t = _extract_user_text(record)
            if t:
                title = t[:TITLE_MAX_LEN].strip()
                first_user = t
    return title, cwd, event_count, first_user


def _extract_user_text(record: dict[str, Any]) -> str:
    if record.get("type") != "user":
        return ""
    msg = record.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    return block["text"]
                if isinstance(block.get("text"), str):
                    return block["text"]
    return ""


def _decode_cwd_from_dir(dir_name: str) -> str:
    """Best-effort reverse of claude's `C--Users-darkh-Projects-foo` encoding."""
    if not dir_name:
        return ""
    if len(dir_name) > 2 and dir_name[1:3] == "--":
        drive = dir_name[0] + ":"
        rest = dir_name[3:].replace("-", "\\")
        return drive + "\\" + rest
    return dir_name.replace("-", "/")


def find_session_jsonl(session_id: str, projects_root: Path | None = None) -> Path | None:
    """Locate the .jsonl for a session id by walking the projects root."""
    root = projects_root or default_projects_root()
    if not root.is_dir():
        return None
    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None
