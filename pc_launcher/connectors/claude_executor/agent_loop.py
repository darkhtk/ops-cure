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

import hashlib
import json
import mimetypes
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .claude_runtime import ClaudeRun


# Socket timeout applies to BOTH connect and per-read operations on the
# urllib stream. The bridge sends SSE heartbeats every ~15s, so 60s gives
# plenty of margin for transient TCP buffering or scheduling jitter.
_SSE_OPEN_TIMEOUT_SECONDS = 60.0
_SSE_RECONNECT_BACKOFF_BASE_SECONDS = 1.0   # exponential backoff base
_SSE_RECONNECT_BACKOFF_MAX_SECONDS = 60.0   # cap
_RUN_RESULT_TIMEOUT_SECONDS = 180.0
_HEARTBEAT_INTERVAL_SECONDS = 60.0


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
        # Configuration knobs:
        #   history_limit    — pre-fetch this many recent op events
        #                      before each run so the prompt carries
        #                      conversational context. 0 = off.
        #   system_prompt    — persona-specific guidance prepended to
        #                      every prompt.
        #   actor_token      — optional v3 phase-3.x bound token. When
        #                      set, sent as ``X-Actor-Token`` on every
        #                      mutating request so the bridge can prove
        #                      this loop really speaks for ``actor_handle``.
        # Phase 3 cleanup: ``broadcast`` and ``max_responses_per_op``
        # were retired. Cascade prevention is now mechanical via the
        # bridge's ``expected_response.from_actor_handles`` contract;
        # the op-level cap ``policy.max_rounds`` replaces the per-
        # persona client guard. Callers who used those knobs before
        # should set ``policy.max_rounds`` on the op and address
        # responders explicitly via ``expected_response``.
        history_limit: int = 0,
        system_prompt: str | None = None,
        actor_token: str | None = None,
    ) -> None:
        if not actor_handle.startswith("@"):
            actor_handle = f"@{actor_handle}"
        self._bridge_url = bridge_url.rstrip("/")
        self._token = token
        self._actor_token = actor_token
        self._actor_handle = actor_handle
        self._cwd = cwd
        self._model = model
        self._permission_mode = permission_mode
        self._on_log = on_log or (lambda msg: print(msg, file=sys.stderr))

        self._history_limit = max(0, int(history_limit))
        self._system_prompt = (system_prompt or "").strip()

        self._actor_id: str | None = None
        self._stopping = False
        self._sse_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._event_q: queue.Queue[dict[str, Any]] = queue.Queue()
        self._seen_event_ids: set[str] = set()  # idempotency
        # Traceparent of the SSE session — captured from the response
        # headers when present so subsequent POSTs inherit the trace.
        self._sse_traceparent: str | None = None
        # D2 — record the most recent post rejection per op so the
        # next prompt can surface ``Your last reply was rejected:
        # <detail>. Adjust your kind/strategy.``. Without this the
        # LLM has no idea its prior speech act was dropped 400.
        # Mapping: op_id → {"detail": str, "rejected_kind": str}.
        self._last_post_rejection: dict[str, dict[str, str]] = {}
        # P9.4 / D14 — claude run that returned no terminal result
        # (LLM hung / timed out / crashed). Same surface pattern as
        # D2 but for the *upstream* failure: agent didn't get to
        # post anything. Without this, the LLM's next turn doesn't
        # realize its previous run was dropped.
        # Mapping: op_id → {"detail": str, "ts": str, "trigger_event_id": str}.
        self._last_run_failure: dict[str, dict[str, str]] = {}

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
        # v3 phase 4: liveness heartbeat. The bridge already sees
        # activity from SSE / POSTs implicitly, but during long idle
        # periods a periodic ping keeps last_seen_at fresh so
        # presence consumers don't false-positive "agent dead".
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"agent-loop-heartbeat:{self._actor_handle}",
            daemon=True,
        )
        self._sse_thread.start()
        self._worker_thread.start()
        self._heartbeat_thread.start()
        self._log(f"agent loop started: handle={self._actor_handle} cwd={self._cwd}")

    def stop(self) -> None:
        self._stopping = True
        # Worker drains via sentinel; SSE / heartbeat threads break on
        # next loop iteration (they check _stopping).
        self._event_q.put({"_sentinel": True})

    # ---- heartbeat ----------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self._stopping:
            # Sleep first so we don't double-ping on startup (the SSE
            # subscribe + first inbox event already proves liveness).
            for _ in range(int(_HEARTBEAT_INTERVAL_SECONDS)):
                if self._stopping:
                    return
                time.sleep(1)
            try:
                self._post_heartbeat()
            except Exception as e:  # noqa: BLE001
                self._log(f"heartbeat error: {e}")

    def _post_heartbeat(self) -> None:
        url = (
            f"{self._bridge_url}/v2/actors/"
            f"{urllib.request.quote(self._actor_handle.lstrip('@'))}/heartbeat"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if self._actor_token:
            headers["X-Actor-Token"] = self._actor_token
        if self._sse_traceparent:
            headers["traceparent"] = self._sse_traceparent
        req = urllib.request.Request(
            url,
            data=b"{}",  # body is empty JSON object
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            resp.read()

    # ---- SSE reader ---------------------------------------------------

    def _sse_loop(self) -> None:
        # v3 phase 4: exponential backoff + jitter so 30 agents
        # reconnecting after a bridge restart don't all hammer at the
        # same instant.
        import random
        attempt = 0
        while not self._stopping:
            try:
                self._consume_one_sse_session()
                attempt = 0  # reset on clean exit
            except Exception as e:  # noqa: BLE001
                attempt += 1
                base = _SSE_RECONNECT_BACKOFF_BASE_SECONDS * min(
                    2 ** (attempt - 1), int(_SSE_RECONNECT_BACKOFF_MAX_SECONDS),
                )
                base = min(base, _SSE_RECONNECT_BACKOFF_MAX_SECONDS)
                jitter = base * (random.random() * 0.2)  # ±10% (×0..×0.2)
                wait = base + jitter
                self._log(
                    f"agent loop SSE error: {e}; reconnect attempt={attempt} "
                    f"in {wait:.1f}s"
                )
                time.sleep(wait)
            if self._stopping:
                return

    def _consume_one_sse_session(self) -> None:
        url = (
            f"{self._bridge_url}/v2/inbox/stream"
            f"?actor_handle={urllib.request.quote(self._actor_handle)}"
        )
        sse_headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {self._token}",
        }
        if self._actor_token:
            sse_headers["X-Actor-Token"] = self._actor_token
        req = urllib.request.Request(
            url,
            method="GET",
            headers=sse_headers,
        )
        with urllib.request.urlopen(req, timeout=_SSE_OPEN_TIMEOUT_SECONDS) as resp:
            # Capture the bridge's session traceparent so all POSTs
            # for this SSE session inherit the same trace_id (the
            # span_id changes per request -- that's the bridge's job).
            tp = resp.headers.get("traceparent")
            if tp:
                self._sse_traceparent = tp
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
        # v3 routing — phase 3 cleanup: response gate is now purely
        # mechanical, no broadcast fallback.
        #
        # 1. If the speaker declared ``expected_response`` (the
        #    canonical path in v3): respond iff my handle is listed
        #    in ``expected_response.from_actor_handles``.
        # 2. Otherwise (no expected_response on the event): respond
        #    iff I'm explicitly in ``addressed_to_actor_ids``. An
        #    event with no addressee at all is treated as a public
        #    announcement that does NOT obligate any agent reply.
        ex = ev.get("expected_response")
        if isinstance(ex, dict):
            wanted = ex.get("from_actor_handles") or []
            if self._actor_handle not in wanted:
                return
        else:
            addressed = ev.get("addressed_to_actor_ids") or []
            if not addressed:
                return  # no explicit responder → no auto-reply
            if self._actor_id and self._actor_id not in addressed:
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
        # v3 phase 2.5: skip closed ops up-front. SSE may deliver an
        # event from a closing op (race) or a stale queue entry from
        # before close; running claude on those wastes a turn whose
        # POST will be rejected by the bridge anyway. Best-effort
        # check — if the op probe fails we proceed (the POST gate is
        # the authoritative refusal).
        if self._op_is_closed(op_id):
            self._log(f"agent loop: skipping op={op_id} (closed)")
            return
        prompt = self._build_prompt(ev)
        if not prompt.strip():
            return
        result_text = self._run_claude_blocking(prompt)
        if not result_text:
            # P9.4 / D14 — record the run failure so the next time
            # this op surfaces an event, the prompt can include a
            # ⚠️ marker. Without it the LLM has no idea its prior
            # turn produced no output and is liable to repeat the
            # same heavy prompt that hung.
            self._last_run_failure[op_id] = {
                "detail": "Your previous claude run produced no terminal result "
                          "(timed out or crashed before emitting a reply). "
                          "Try a smaller scope this turn or split the work into "
                          "narrower [EVIDENCE] events.",
                "trigger_event_id": ev.get("event_id") or "",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._log(
                f"agent loop: run produced no result for op={op_id} "
                f"ev={ev.get('event_id')} (recorded for next-turn surface)"
            )
            return
        # v3-additive: link reply to the trigger event so the bridge's
        # reply chain (replies_to_event_id) is no longer dead.
        trigger_event_id = ev.get("event_id")
        self._post_claim(op_id, result_text, in_reply_to=trigger_event_id)

    def _auth_headers(self) -> dict[str, str]:
        """Headers used on read-only / GET-shaped requests. POST/SSE
        paths copy these and add Content-Type / Accept as needed."""
        h = {"Authorization": f"Bearer {self._token}"}
        if self._actor_token:
            h["X-Actor-Token"] = self._actor_token
        # Forward the SSE session's traceparent so bridge-side logs
        # link "event delivered to agent" → "claim posted by agent"
        # under one trace_id.
        if self._sse_traceparent:
            h["traceparent"] = self._sse_traceparent
        return h

    def _op_is_closed(self, op_id: str) -> bool:
        """Best-effort op state probe. Returns True only when we have a
        confident answer that the op is closed; transient errors return
        False so we don't drop legitimate work on a flaky network."""
        url = f"{self._bridge_url}/v2/operations/{urllib.request.quote(op_id)}"
        req = urllib.request.Request(
            url, method="GET",
            headers=self._auth_headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                body = resp.read()
        except Exception:  # noqa: BLE001
            return False
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, TypeError):
            return False
        return data.get("state") == "closed"

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
            headers=self._auth_headers(),
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
        # D2 — surface any HTTP 400 rejection from this actor's
        # most recent post on the same op. Without this the LLM
        # has no idea its prior speech act was dropped and tends
        # to repeat the same shape, deadlocking the conversation.
        rejected = self._last_post_rejection.get(op_id)
        if rejected:
            lines.append("")
            lines.append(
                "⚠️ Your previous reply on this op was REJECTED by "
                f"the bridge (HTTP {rejected.get('http_status','400')}, "
                f"kind={rejected.get('rejected_kind','?')}):"
            )
            lines.append(f"  {rejected.get('detail','')}")
            lines.append(
                "Adjust your reply to satisfy that constraint. Common "
                "moves: pick a kind on the whitelist, switch to "
                "[OBJECT] / [DEFER] / [EVIDENCE] (universal carve-outs), "
                "or rephrase if the bridge cited a payload-shape error."
            )
        # P9.4 / D14 — surface the prior run-failure (claude hung
        # / timed out / produced no terminal text). Same pattern as
        # D2 but for the upstream LLM hang rather than the
        # downstream bridge rejection.
        run_fail = self._last_run_failure.get(op_id)
        if run_fail:
            lines.append("")
            lines.append(
                "⚠️ Your PREVIOUS claude run on this op did not produce "
                "any output (LLM hang / timeout / crash):"
            )
            lines.append(f"  {run_fail.get('detail','')}")
            lines.append(
                "Don't repeat the same heavy prompt. Pick a smaller "
                "deliverable for this turn, or [DEFER→@<someone>] if "
                "you need help."
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
        # D8 — when the trigger event narrows the reply kind, surface
        # that to the LLM so it can pick a satisfying prefix instead
        # of guessing and getting rejected.
        trigger_ex = ev.get("expected_response") or {}
        trigger_kinds = trigger_ex.get("kinds") or []
        if trigger_kinds and "*" not in trigger_kinds:
            lines.append(
                f"⚙️ The trigger declared expected_response.kinds="
                f"{trigger_kinds}. Your reply MUST use one of these "
                f"kinds OR a universal carve-out (object / evidence / "
                f"defer). Other kinds will be rejected with HTTP 400."
            )
            lines.append("")
        lines.append(
            "Respond in 1-3 sentences. The prefix controls the speech "
            "kind AND the next-responder contract.\n"
            "\n"
            "🔒 STRICT FORMAT: the `[KIND]` prefix MUST be the very\n"
            "FIRST non-whitespace characters of your reply. The parser\n"
            "only reads position 0. Anything before the `[` (even an\n"
            "intro sentence) makes the parser fall back to plain CLAIM\n"
            "and your `[EVIDENCE]` / `[PROPOSE]` etc. becomes prose. If\n"
            "your message has multiple structured prefixes, post them\n"
            "as separate events.\n"
            "\n"
            "  [KIND] body...                — TERMINAL. No specific\n"
            "                                  next-responder; the reply\n"
            "                                  stands on its own.\n"
            "\n"
            "  [KIND→@a,@b] body...          — INVITING. Names actors\n"
            "                                  that should respond next.\n"
            "                                  Use when your reply only\n"
            "                                  matters if someone acts.\n"
            "\n"
            "  [KIND→@a kinds=ratify,object] — INVITING + restrict reply\n"
            "                                  kinds (optional).\n"
            "\n"
            "KIND ∈ [CLAIM] [QUESTION] [PROPOSE] [AGREE] [OBJECT] [REACT]\n"
            "       [RATIFY] [MOVE_CLOSE] [DEFER] [INVITE] [JOIN]. Default CLAIM.\n"
            "\n"
            "Use INVITING when:\n"
            "  - you propose something (name who should agree/object/ratify)\n"
            "  - you ask a question (name the addressee)\n"
            "  - you finish a sub-step (name who picks up next)\n"
            "Use TERMINAL when:\n"
            "  - chiming in / observing / acknowledging\n"
            "  - the conversation is complete\n"
            "When in doubt, prefer TERMINAL. Silence > false invitation.\n"
            "\n"
            "If you have nothing useful to add, reply with exactly: SKIP"
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
        # v3 governance acts.
        "move_close", "ratify",
        # v3 phase 2.5 membership acts.
        "invite", "join",
    }

    @classmethod
    def _parse_reply_prefix(
        cls, text: str
    ) -> tuple[str, dict[str, Any] | None, str]:
        """Parse the agent's reply prefix into ``(kind, expected_response, body)``.

        Recognized forms:

        - ``[KIND] body``                          — TERMINAL, expected_response=None
        - ``[KIND@a,@b] body``                     — INVITING, from=[@a,@b]
        - ``[KIND→@a,@b] body``                    — same (unicode arrow)
        - ``[KIND→@a kinds=foo,bar] body``         — INVITING + kind whitelist
        - ``[KIND→@a kinds=*] body``               — INVITING + any kind

        If KIND isn't recognized, the prefix is left as part of the body
        and ``kind="claim"`` is returned (legacy fallback). If no prefix
        at all, also legacy claim with no expected_response.

        This parser is the *only* protocol-aware step in the reply path:
        it converts free-form LLM output into a structured speech event
        + optional reply contract. The protocol itself does not bake
        any workflow assumptions; the agent's prefix declares them.
        """
        cleaned = text.strip()
        if not cleaned.startswith("["):
            return "claim", None, cleaned
        # Find matching `]` within a bounded prefix window. Prefix may
        # legitimately include `→`, `@handle`, `kinds=...`; cap at 200
        # chars (inclusive of `]`) to avoid pathological inputs.
        # ``str.find(s, 1, 201)`` searches indices 1..200, matching the
        # TS reference impl which accepts `end <= 200`. Pre-T1.1 the
        # bound was 200 exclusive, which produced cross-impl drift on
        # the exact-200 boundary (probe A in parser-probe.test.ts).
        end = cleaned.find("]", 1, 201)
        if end == -1:
            return "claim", None, cleaned
        inside = cleaned[1:end].strip()
        body = cleaned[end + 1 :].lstrip()
        # Split into kind + arrow-tail. Both `→` and `->` accepted.
        # `[KIND@a,@b]` (no arrow) is also tolerated.
        for sep in ("→", "->", "@"):
            if sep in inside:
                kind_raw, _, tail = inside.partition(sep)
                tail = sep + tail if sep == "@" else tail
                kind = kind_raw.strip().lower()
                if kind not in cls._ALLOWED_SPEECH_KINDS:
                    return "claim", None, cleaned
                # Parse handles + optional kinds from tail
                handles, allowed_kinds = cls._parse_invite_tail(tail)
                if not handles:
                    return kind, None, body
                ex: dict[str, Any] = {"from_actor_handles": handles}
                if allowed_kinds:
                    ex["kinds"] = allowed_kinds
                return kind, ex, body
        # No arrow / @ marker — pure TERMINAL prefix
        kind = inside.lower()
        if kind not in cls._ALLOWED_SPEECH_KINDS:
            return "claim", None, cleaned
        return kind, None, body

    # T1.2 — out-of-band artifact descriptor for ``speech.evidence``.
    # The agent prepends ``ARTIFACT: path=relative/file [kind=code] [label=...]``
    # as the FIRST LINE of the reply body (after the prefix). The
    # parser pulls it off, stats the file under ``agent_cwd``, and
    # forwards a structured ``payload.artifact`` to the bridge. The
    # bridge auto-creates an OperationArtifact row tied to the event.
    #
    # Why a header line and not an extension to ``[KIND→…]``? The
    # prefix parser is now under cross-impl conformance (Python ↔ TS)
    # — every grammar widening has to be mirrored. Evidence-with-
    # artifact is a Python-side convenience: the WIRE shape
    # (``payload.artifact = {kind, uri, sha256, ...}``) is what the
    # protocol cares about. Other clients are free to compute the
    # artifact dict however they want.

    _ARTIFACT_HEADER_PREFIX = "ARTIFACT:"

    def _maybe_extract_artifacts(
        self, body: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """P9.3 / D11 — consume CONSECUTIVE ``ARTIFACT: ...`` header
        lines from the start of ``body`` and return the list of
        normalized artifact dicts plus the remaining body text.

        The single-artifact form (T1.2) is preserved as the special
        case ``len(out) == 1``. Pre-P9.3 callers received a tuple
        ``(dict | None, rest)``; the new shape is ``(list, rest)``.
        Update call sites in ``_post_claim`` accordingly.
        """
        artifacts: list[dict[str, Any]] = []
        rest = body
        while rest.startswith(self._ARTIFACT_HEADER_PREFIX):
            art, rest = self._maybe_extract_artifact(rest)
            if art is None:
                # The line started with ``ARTIFACT:`` but didn't
                # parse to a usable dict (missing path, unreadable
                # file). _maybe_extract_artifact already stripped
                # the malformed header line and logged a warning;
                # don't keep looping on whatever rest now starts with.
                break
            artifacts.append(art)
        return artifacts, rest

    def _maybe_extract_artifact(
        self, body: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """If the first line of ``body`` is an ``ARTIFACT: path=...``
        header, stat the referenced file under ``agent_cwd`` and
        return ``({kind, uri, sha256, mime, size_bytes, label?}, rest_body)``.

        Otherwise ``(None, body)``. Failures (path missing,
        unreadable) are logged and treated as no-op — the speech
        event still goes through with the original body.
        """
        if not body.startswith(self._ARTIFACT_HEADER_PREFIX):
            return None, body
        first_line, sep, rest = body.partition("\n")
        spec_str = first_line[len(self._ARTIFACT_HEADER_PREFIX):].strip()
        # Tokens: ``key=value``. Values are bare (no quoting) so paths
        # MUST NOT contain spaces. Most agent cwd file paths satisfy
        # this; if not, the agent should rename or wrap differently.
        fields: dict[str, str] = {}
        for tok in spec_str.split():
            if "=" not in tok:
                continue
            k, _, v = tok.partition("=")
            k = k.strip()
            v = v.strip()
            if k and v:
                fields[k] = v
        path = fields.get("path")
        if not path:
            self._log(
                "agent loop: ARTIFACT header missing path= field; ignoring "
                f"({spec_str!r})"
            )
            return None, rest.lstrip("\n") if sep else body
        cwd = Path(self._cwd) if self._cwd else Path.cwd()
        full = (cwd / path).resolve()
        try:
            if not full.is_file():
                raise FileNotFoundError(f"not a file: {full}")
        except (OSError, FileNotFoundError) as exc:
            self._log(
                f"agent loop: artifact path {full} unreadable ({exc}); "
                "skipping attachment"
            )
            return None, rest.lstrip("\n") if sep else body
        try:
            h = hashlib.sha256()
            size = 0
            with full.open("rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    h.update(chunk)
                    size += len(chunk)
            sha256 = h.hexdigest()
        except OSError as exc:
            self._log(
                f"agent loop: artifact stat failed for {full} ({exc}); "
                "skipping attachment"
            )
            return None, rest.lstrip("\n") if sep else body
        mime, _ = mimetypes.guess_type(str(full))
        if mime is None:
            mime = "application/octet-stream"
        artifact: dict[str, Any] = {
            "kind": fields.get("kind", "file"),
            "uri": full.as_uri(),
            "sha256": sha256,
            "mime": mime,
            "size_bytes": size,
        }
        if "label" in fields:
            artifact["label"] = fields["label"]
        return artifact, rest.lstrip("\n") if sep else ""

    @staticmethod
    def _parse_invite_tail(tail: str) -> tuple[list[str], list[str]]:
        """Pull ``@handle`` tokens + optional ``kinds=foo,bar`` from a
        bracket-prefix tail. Robust against trailing whitespace and
        comma variants."""
        # Detect "kinds=..." segment
        kinds: list[str] = []
        if "kinds=" in tail:
            head, _, kinds_str = tail.partition("kinds=")
            tail = head
            kinds = [k.strip() for k in kinds_str.replace(" ", ",").split(",") if k.strip()]
        # Pull every @handle in remaining tail
        handles: list[str] = []
        for token in tail.replace(",", " ").split():
            t = token.strip().rstrip(",;")
            if t.startswith("@") and len(t) > 1:
                if t not in handles:
                    handles.append(t)
        return handles, kinds

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
        # v3 phase 6: prefix carries kind AND optional next-responder
        # contract. See _parse_reply_prefix for the grammar.
        kind, expected_response, cleaned = self._parse_reply_prefix(cleaned)
        # T1.2 + P9.3 / D11: evidence may carry ONE OR MORE
        # out-of-band ``ARTIFACT: path=...`` headers on consecutive
        # leading body lines. Strip + stat each before posting so
        # the bridge auto-creates one OperationArtifact row per
        # listed file.
        artifacts: list[dict[str, Any]] = []
        if kind == "evidence":
            artifacts, cleaned = self._maybe_extract_artifacts(cleaned)
        url = f"{self._bridge_url}/v2/operations/{op_id}/events"
        payload: dict[str, Any] = {"text": cleaned}
        if len(artifacts) == 1:
            # Preserve the singular ``payload.artifact`` shape on the
            # wire when there's only one — keeps existing T1.2
            # consumers happy without forcing them to handle the list.
            payload["artifact"] = artifacts[0]
        elif len(artifacts) > 1:
            payload["artifacts"] = artifacts
        body: dict[str, Any] = {
            "actor_handle": self._actor_handle,
            "kind": f"speech.{kind}",
            "payload": payload,
        }
        if expected_response is not None:
            body["expected_response"] = expected_response
        if in_reply_to:
            # The /v2/operations/{id}/events endpoint accepts
            # ``replies_to_event_id`` -- v3-additive: this is now always
            # populated when we're replying to an inbox event so the
            # bridge can reconstruct disagreement / proposal chains.
            body["replies_to_event_id"] = in_reply_to
        data = json.dumps(body).encode("utf-8")
        post_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if self._actor_token:
            post_headers["X-Actor-Token"] = self._actor_token
        if self._sse_traceparent:
            post_headers["traceparent"] = self._sse_traceparent
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers=post_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                resp.read()
            invite_summary = ""
            if expected_response and expected_response.get("from_actor_handles"):
                invite_summary = (
                    f" inviting={expected_response['from_actor_handles']}"
                )
            self._log(
                f"agent loop: posted speech.{kind} to op={op_id} "
                f"len={len(cleaned)}{invite_summary}"
            )
            # D2 — successful post clears any prior rejection so the
            # next prompt doesn't carry stale "your last reply was
            # rejected" warnings into a fresh conversation turn.
            self._last_post_rejection.pop(op_id, None)
            # P9.4 / D14 — same idea for the upstream run-failure
            # marker. A successful post implies the run produced
            # text this time; the prior hang is no longer relevant.
            self._last_run_failure.pop(op_id, None)
            return True
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                detail = ""
            self._log(f"agent loop: claim post failed op={op_id} HTTP {e.code}: {detail}")
            # D2 — capture the rejection so the next prompt can show
            # the LLM what went wrong. Without this the agent silently
            # loses messages and the conversation deadlocks (RPG smoke
            # observation 2026-05-04). 4xx is a contract violation
            # the LLM can correct (kind whitelist, payload shape, etc.);
            # 5xx is bridge-side, not directly actionable, so we still
            # capture but the prompt phrasing makes that clear.
            if 400 <= e.code < 500:
                # Strip the typical FastAPI envelope ``{"detail": "..."}``
                # to a flat string so the prompt isn't cluttered.
                detail_text = detail
                try:
                    parsed = json.loads(detail or "")
                    if isinstance(parsed, dict) and "detail" in parsed:
                        d = parsed["detail"]
                        detail_text = d if isinstance(d, str) else json.dumps(d)
                except Exception:  # noqa: BLE001
                    pass
                self._last_post_rejection[op_id] = {
                    "detail": detail_text[:500],
                    "rejected_kind": kind,
                    "http_status": str(e.code),
                }
            return False
        except urllib.error.URLError as e:
            self._log(f"agent loop: claim post network error op={op_id}: {e.reason}")
            return False

    # ---- helpers ------------------------------------------------------

    def _log(self, msg: str) -> None:
        self._on_log(f"[agent-loop] {msg}")
