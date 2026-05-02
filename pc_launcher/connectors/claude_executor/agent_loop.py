"""External agent loop — connects this PC's claude_executor to the bridge
as a kernel-level participant.

The bridge is the kernel; agents are external clients. This module
implements the agent half of that contract:

  1. Open an SSE stream on /v2/inbox/stream for our actor handle.
  2. For each chat.speech.* event addressed to us (and not authored by
     us), build a prompt and run claude locally via ClaudeRun.
  3. When claude emits the terminal {type:"result", subtype:"success"}
     frame, POST the result back as a speech.claim via
     /v2/operations/{op_id}/events.

Multi-agent = multiple BridgeAgentLoop instances on multiple PCs, each
with a distinct actor handle. The bridge does no per-agent configuration
and is unaware of brains; it just routes events to actors that have
subscribed to their inbox.

The legacy remote_claude command-claim loop (browser-driven runs) keeps
running in parallel in agent.py; the two paths share nothing except the
ClaudeRun runtime.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .claude_runtime import ClaudeRun


_SSE_OPEN_TIMEOUT_SECONDS = 30.0
_SSE_RECONNECT_BACKOFF_SECONDS = 5.0
_RUN_RESULT_TIMEOUT_SECONDS = 180.0


class BridgeAgentLoop:
    """One external agent. Subscribes to its inbox, runs claude on each
    addressed speech event, posts the result back as a speech.claim.
    """

    def __init__(
        self,
        *,
        bridge_url: str,
        token: str,
        actor_handle: str,
        cwd: str,
        model: str | None = None,
        permission_mode: str | None = "acceptEdits",
        on_log=None,
    ) -> None:
        if not actor_handle.startswith("@"):
            actor_handle = f"@{actor_handle}"
        self._bridge_url = bridge_url.rstrip("/")
        self._token = token
        self._actor_handle = actor_handle
        self._cwd = cwd
        self._model = model
        self._permission_mode = permission_mode
        self._on_log = on_log or (lambda msg: print(msg, file=sys.stderr))

        self._actor_id: str | None = None
        self._stopping = False
        self._sse_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._event_q: queue.Queue[dict[str, Any]] = queue.Queue()
        self._seen_event_ids: set[str] = set()  # idempotency

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> None:
        self._sse_thread = threading.Thread(
            target=self._sse_loop,
            name=f"agent-loop-sse:{self._actor_handle}",
            daemon=True,
        )
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name=f"agent-loop-worker:{self._actor_handle}",
            daemon=True,
        )
        self._sse_thread.start()
        self._worker_thread.start()
        self._log(f"agent loop started: handle={self._actor_handle} cwd={self._cwd}")

    def stop(self) -> None:
        self._stopping = True
        # Worker drains via sentinel; SSE thread breaks on next read.
        self._event_q.put({"_sentinel": True})

    # ---- SSE reader ---------------------------------------------------

    def _sse_loop(self) -> None:
        while not self._stopping:
            try:
                self._consume_one_sse_session()
            except Exception as e:  # noqa: BLE001
                self._log(f"agent loop SSE error: {e}; reconnecting in "
                          f"{_SSE_RECONNECT_BACKOFF_SECONDS}s")
            if self._stopping:
                return
            time.sleep(_SSE_RECONNECT_BACKOFF_SECONDS)

    def _consume_one_sse_session(self) -> None:
        url = (
            f"{self._bridge_url}/v2/inbox/stream"
            f"?actor_handle={urllib.request.quote(self._actor_handle)}"
        )
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self._token}",
            },
        )
        with urllib.request.urlopen(req, timeout=_SSE_OPEN_TIMEOUT_SECONDS) as resp:
            self._log(f"agent loop connected to {url}")
            event_name = ""
            data_lines: list[str] = []
            for raw in resp:
                if self._stopping:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if event_name and data_lines:
                        self._dispatch_sse_frame(event_name, "\n".join(data_lines))
                    event_name = ""
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue  # SSE comment (heartbeat)
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())

    def _dispatch_sse_frame(self, event_name: str, data: str) -> None:
        try:
            payload = json.loads(data)
        except (ValueError, TypeError):
            return
        if event_name == "open":
            self._actor_id = payload.get("actor_id")
            self._log(
                f"agent loop subscribed: actor_id={self._actor_id} "
                f"space={payload.get('space_id')}"
            )
            return
        if event_name == "v2.event":
            self._enqueue_event(payload)
            return
        # heartbeat / unknown -> ignore

    def _enqueue_event(self, ev: dict[str, Any]) -> None:
        kind = str(ev.get("kind") or "")
        if not kind.startswith("chat.speech."):
            return
        # Self-loop guard: never respond to my own speech.
        if self._actor_id and ev.get("actor_id") == self._actor_id:
            return
        # Addressing: respond only if I'm in addressed_to_actor_ids
        # (or list is empty == public broadcast — skip in agent mode).
        addressed = ev.get("addressed_to_actor_ids") or []
        if self._actor_id and self._actor_id not in addressed:
            return
        # Skip my own claims (server-side fanout sometimes echos).
        if kind == "chat.speech.claim" and ev.get("actor_id") == self._actor_id:
            return
        # Idempotency.
        eid = ev.get("event_id")
        if eid and eid in self._seen_event_ids:
            return
        if eid:
            self._seen_event_ids.add(eid)
        self._event_q.put(ev)

    # ---- worker (one claude run at a time) ----------------------------

    def _worker_loop(self) -> None:
        while not self._stopping:
            ev = self._event_q.get()
            if ev.get("_sentinel"):
                return
            try:
                self._handle_event(ev)
            except Exception as e:  # noqa: BLE001
                self._log(f"agent loop worker error on event "
                          f"{ev.get('event_id')}: {e}")

    def _handle_event(self, ev: dict[str, Any]) -> None:
        op_id = ev.get("operation_id")
        if not op_id:
            return
        prompt = self._build_prompt(ev)
        if not prompt.strip():
            return
        result_text = self._run_claude_blocking(prompt)
        if not result_text:
            self._log(
                f"agent loop: run produced no result for op={op_id} ev={ev.get('event_id')}"
            )
            return
        self._post_claim(op_id, result_text)

    def _build_prompt(self, ev: dict[str, Any]) -> str:
        payload = ev.get("payload") or {}
        text = str(payload.get("text") or "").strip()
        if not text:
            return ""
        # Minimal protocol context. The bridge already enforces addressing
        # and op state — we just need the speaker's question.
        kind = str(ev.get("kind") or "chat.speech")
        return (
            f"You are agent {self._actor_handle} responding to a "
            f"{kind} event in operation {ev.get('operation_id')}. "
            f"The speaker said:\n\n{text}\n\n"
            f"Respond directly and concisely. Your reply will be posted "
            f"as a speech.claim back to this operation."
        )

    def _run_claude_blocking(self, prompt: str) -> str:
        """Spawn a fresh claude run, write the prompt, wait for the
        terminal result frame, and return the text. Each event gets its
        own short-lived claude session — we don't keep multi-turn state
        on the agent side (it's stored in the v2 op's event log instead).
        """
        result_holder: dict[str, str] = {}
        done = threading.Event()

        def on_event(wrapped: dict[str, Any]) -> None:
            # ClaudeRun emits wrapped {"kind": "claude.event", "event": <stream-json>}
            # plus {"kind": "claude.exit"} / {"kind": "claude.stderr"} envelopes.
            if wrapped.get("kind") != "claude.event":
                return
            inner = wrapped.get("event") or {}
            if not isinstance(inner, dict) or inner.get("type") != "result":
                return
            subtype = inner.get("subtype")
            if subtype not in ("success", None, ""):
                done.set()
                return
            text = str(inner.get("result") or "").strip()
            result_holder["text"] = text
            done.set()

        def on_exit(_code: int | None) -> None:
            done.set()

        run = ClaudeRun(
            cwd=self._cwd,
            permission_mode=self._permission_mode,
            model=self._model,
            on_event=on_event,
            on_exit=on_exit,
        )
        try:
            run.spawn()
            run.write_user_message(prompt)
            done.wait(timeout=_RUN_RESULT_TIMEOUT_SECONDS)
        finally:
            try:
                run.close()
            except Exception:  # noqa: BLE001
                pass
        return result_holder.get("text", "")

    def _post_claim(self, op_id: str, text: str) -> None:
        url = f"{self._bridge_url}/v2/operations/{op_id}/events"
        body = {
            "actor_handle": self._actor_handle,
            "kind": "speech.claim",
            "payload": {"text": text},
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                resp.read()
            self._log(f"agent loop: posted claim to op={op_id} len={len(text)}")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                detail = ""
            self._log(f"agent loop: claim post failed op={op_id} HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            self._log(f"agent loop: claim post network error op={op_id}: {e.reason}")

    # ---- helpers ------------------------------------------------------

    def _log(self, msg: str) -> None:
        self._on_log(f"[agent-loop] {msg}")
