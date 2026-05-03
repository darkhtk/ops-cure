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


# Socket timeout applies to BOTH connect and per-read operations on the
# urllib stream. The bridge sends SSE heartbeats every ~15s, so 60s gives
# plenty of margin for transient TCP buffering or scheduling jitter.
_SSE_OPEN_TIMEOUT_SECONDS = 60.0
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
        # Extension knobs (a/b/c from the protocol triage):
        #   broadcast        — also respond to events with no
        #                      addressed_to_actor_ids (room-wide speech).
        #                      Default off; opt in for collab personas.
        #   history_limit    — pre-fetch this many recent op events
        #                      before each run so the prompt carries
        #                      conversational context (option b). 0 = off.
        #   max_responses_per_op — cap per-op replies as a runaway guard
        #                          when broadcast is on. 0 = unlimited.
        #   system_prompt    — persona-specific guidance prepended to
        #                      every prompt. Lets one executable host
        #                      different personas via env (option c).
        broadcast: bool = False,
        history_limit: int = 0,
        max_responses_per_op: int = 5,
        system_prompt: str | None = None,
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

        self._broadcast = broadcast
        self._history_limit = max(0, int(history_limit))
        self._max_responses_per_op = max(0, int(max_responses_per_op))
        self._system_prompt = (system_prompt or "").strip()

        self._actor_id: str | None = None
        self._stopping = False
        self._sse_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._event_q: queue.Queue[dict[str, Any]] = queue.Queue()
        self._seen_event_ids: set[str] = set()  # idempotency
        self._responses_per_op: dict[str, int] = {}  # op_id -> count

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
        # v3-additive routing:
        #
        # If the speaker declared an explicit reply contract via
        # ``expected_response`` (kernel.v2.contract.validate_expected_response),
        # that wins -- respond iff my handle is listed in
        # ``expected_response.from_actor_handles``. Otherwise the
        # cascade-prevention is mechanical and broadcast/cap heuristics
        # don't get to second-guess it.
        #
        # When expected_response is absent (legacy v2 events), fall back
        # to the original logic: explicit address wins, empty address
        # only triggers when ``BROADCAST=true``.
        ex = ev.get("expected_response")
        if isinstance(ex, dict):
            wanted = ex.get("from_actor_handles") or []
            if self._actor_handle not in wanted:
                return
        else:
            addressed = ev.get("addressed_to_actor_ids") or []
            if addressed:
                if self._actor_id and self._actor_id not in addressed:
                    return
            elif not self._broadcast:
                return
        # Per-op runaway guard (matters mostly when broadcast=True).
        op_id = ev.get("operation_id")
        if (
            self._max_responses_per_op
            and op_id
            and self._responses_per_op.get(op_id, 0) >= self._max_responses_per_op
        ):
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
        # v3-additive: link reply to the trigger event so the bridge's
        # reply chain (replies_to_event_id) is no longer dead.
        trigger_event_id = ev.get("event_id")
        if self._post_claim(op_id, result_text, in_reply_to=trigger_event_id):
            self._responses_per_op[op_id] = self._responses_per_op.get(op_id, 0) + 1

    def _fetch_op_history(self, op_id: str) -> list[dict[str, Any]]:
        """Pull the last `history_limit` events from the op so the prompt
        carries enough context for the persona to reason. Returns [] on
        any failure — context is best-effort, never fatal.
        """
        if self._history_limit <= 0:
            return []
        url = (
            f"{self._bridge_url}/v2/operations/{urllib.request.quote(op_id)}/events"
            f"?actor_handle={urllib.request.quote(self._actor_handle)}"
        )
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                body = resp.read()
        except Exception as e:  # noqa: BLE001 - includes socket.timeout
            # History is best-effort context. If the fetch fails (timeout,
            # transient bridge contention, etc) we just compose the prompt
            # without history rather than abandon the whole run.
            self._log(f"agent loop: op history fetch failed op={op_id}: {e}")
            return []
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, TypeError):
            return []
        events = data.get("events") or []
        if not isinstance(events, list):
            return []
        # Drop the trigger event itself (we'll quote it explicitly).
        return events[-self._history_limit :]

    def _build_prompt(self, ev: dict[str, Any]) -> str:
        payload = ev.get("payload") or {}
        text = str(payload.get("text") or "").strip()
        if not text:
            return ""
        kind = str(ev.get("kind") or "chat.speech")
        op_id = ev.get("operation_id") or ""
        history = self._fetch_op_history(op_id)

        lines: list[str] = []
        if self._system_prompt:
            lines.append(self._system_prompt)
            lines.append("")
        lines.append(
            f"You are {self._actor_handle} in operation {op_id}. "
            f"Your reply will be posted as a chat.speech.claim event."
        )
        if history:
            lines.append("")
            lines.append("Recent op transcript (oldest first):")
            for h in history:
                actor = str(h.get("actor_id") or "?")[:8]
                hkind = str(h.get("kind") or "")
                htext = str((h.get("payload") or {}).get("text") or "")[:300]
                lines.append(f"  [{hkind}] actor={actor}: {htext}")
        lines.append("")
        lines.append(f"You were just addressed via {kind}. The trigger message:")
        lines.append("")
        lines.append(text)
        lines.append("")
        lines.append(
            "Respond in 1-3 sentences. You may prefix your reply with one "
            "of [CLAIM] [QUESTION] [PROPOSE] [AGREE] [OBJECT] [REACT] to "
            "control the speech kind (default: CLAIM). If you have nothing "
            "useful to add, reply with exactly: SKIP"
        )
        return "\n".join(lines)

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

    # Speech kinds the bridge accepts. Mirrored from kernel.v2.contract;
    # we deliberately don't import that on the PC side (no shared package)
    # — drift would manifest as a 400 on POST, which we surface in logs.
    _ALLOWED_SPEECH_KINDS = {
        "claim", "question", "answer", "propose", "agree", "object",
        "evidence", "block", "defer", "summarize", "react",
    }

    def _post_claim(
        self,
        op_id: str,
        text: str,
        *,
        in_reply_to: str | None = None,
    ) -> bool:
        # Personas can opt out of speaking by replying with a literal SKIP
        # sentinel — keeps broadcast loops quiet when one persona has
        # nothing to add.
        cleaned = text.strip()
        if cleaned == "SKIP" or cleaned.upper() == "SKIP":
            self._log(f"agent loop: SKIP from {self._actor_handle} on op={op_id}")
            return False
        # Optional [KIND] prefix lets a persona post non-claim speech
        # (object / propose / agree / question / etc.) without each
        # persona needing its own dispatcher. Format: "[OBJECT] body..."
        kind = "claim"
        if cleaned.startswith("[") and "]" in cleaned[:20]:
            tag_end = cleaned.index("]")
            tag = cleaned[1:tag_end].strip().lower()
            if tag in self._ALLOWED_SPEECH_KINDS:
                kind = tag
                cleaned = cleaned[tag_end + 1 :].lstrip()
        url = f"{self._bridge_url}/v2/operations/{op_id}/events"
        body: dict[str, Any] = {
            "actor_handle": self._actor_handle,
            "kind": f"speech.{kind}",
            "payload": {"text": cleaned},
        }
        if in_reply_to:
            # The /v2/operations/{id}/events endpoint accepts
            # ``replies_to_event_id`` -- v3-additive: this is now always
            # populated when we're replying to an inbox event so the
            # bridge can reconstruct disagreement / proposal chains.
            body["replies_to_event_id"] = in_reply_to
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
            self._log(
                f"agent loop: posted speech.{kind} to op={op_id} len={len(cleaned)}"
            )
            return True
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                detail = ""
            self._log(f"agent loop: claim post failed op={op_id} HTTP {e.code}: {detail}")
            return False
        except urllib.error.URLError as e:
            self._log(f"agent loop: claim post network error op={op_id}: {e.reason}")
            return False

    # ---- helpers ------------------------------------------------------

    def _log(self, msg: str) -> None:
        self._on_log(f"[agent-loop] {msg}")
