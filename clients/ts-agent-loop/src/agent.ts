/**
 * Agent dispatch loop. Spec §6.2 + §12.
 *
 * Mirrors pc_launcher/connectors/claude_executor/agent_loop.py's
 * routing logic, deliberately at the SAME contract — a different
 * implementation here exposes any unspoken assumption that
 * agent_loop made.
 *
 * Reply policy (rule-based, not LLM): the point of this client is
 * to validate the WIRE, not the brain.
 *
 *   speech.question → speech.answer with a short canned ack
 *   any other addressed kind → SKIP (no reply)
 *
 * That means an op driver MUST send speech.question + an
 * expected_response that lists THIS actor in from_actor_handles to
 * make the agent reply. Same constraint as agent_loop.
 */

import type { AgentConfig } from "./config.ts";
import type { BridgeClient } from "./http.ts";
import type { InboxEnvelope } from "./types.ts";

export class Agent {
  private readonly cfg: AgentConfig;
  private readonly client: BridgeClient;
  private actorId: string | null = null;
  private readonly seen = new Set<string>();

  constructor(cfg: AgentConfig, client: BridgeClient) {
    this.cfg = cfg;
    this.client = client;
  }

  setActorId(id: string): void {
    this.actorId = id;
  }

  /** Should we respond to this envelope? Mirrors the v3 mechanical
   *  routing in spec §6.2: expected_response wins; otherwise fall
   *  back to addressed_to_actor_ids. Empty addressing = no auto-reply. */
  shouldRespond(env: InboxEnvelope): boolean {
    // 1. only respond to chat.speech.*
    if (!env.kind.startsWith("chat.speech.")) return false;
    // 2. self-loop guard
    if (this.actorId && env.actor_id === this.actorId) return false;
    // 3. expected_response wins when present
    if (env.expected_response) {
      return env.expected_response.from_actor_handles.includes(this.cfg.actorHandle);
    }
    // 4. fall back to addressed_to
    if (env.addressed_to_actor_ids.length === 0) return false;
    return this.actorId !== null && env.addressed_to_actor_ids.includes(this.actorId);
  }

  /** Idempotency check — bridge resends are silently dropped. */
  markSeen(eventId: string): boolean {
    if (this.seen.has(eventId)) return false;
    this.seen.add(eventId);
    return true;
  }

  /** Rule-based reply builder. Returns null when there's nothing
   *  meaningful to say (the WIRE-level "agent had nothing to add").
   *
   *  Reply ``text`` MAY include the v3 phase-6 prefix grammar:
   *
   *    [KIND] body                    — TERMINAL
   *    [KIND→@a,@b] body              — INVITING (next-responders)
   *    [KIND→@a kinds=foo,bar] body   — INVITING + kind whitelist
   *
   *  ``post`` parses the prefix and translates it into structured
   *  ``expected_response`` on the outgoing event so the bridge can
   *  fan-out without further driver intervention. The protocol
   *  itself is workflow-agnostic; the agent (here: rule-based)
   *  decides per-reply who picks up next.
   */
  buildReply(env: InboxEnvelope): { kind: string; text: string } | null {
    const triggerText = (env.payload.text ?? "").toString().slice(0, 200);
    if (env.kind === "chat.speech.question") {
      return {
        kind: "speech.answer",
        text: `Acknowledged via ts-agent-loop: ${triggerText}`,
      };
    }
    return null;
  }

  async post(env: InboxEnvelope, reply: { kind: string; text: string }): Promise<void> {
    const parsed = parseReplyPrefix(reply.text);
    const body: Record<string, unknown> = {
      actor_handle: this.cfg.actorHandle,
      kind: parsed.kind ? `speech.${parsed.kind}` : reply.kind,
      payload: { text: parsed.body },
      replies_to_event_id: env.event_id,
    };
    if (parsed.expectedResponse) {
      body["expected_response"] = parsed.expectedResponse;
    }
    await this.client.postJson(
      `/v2/operations/${env.operation_id}/events`,
      body,
    );
  }
}

/** v3 phase-6 reply prefix grammar — mirror of the Python parser in
 *  ``pc_launcher/connectors/claude_executor/agent_loop.py``. Kept in
 *  sync via the parser conformance test fixture. */
const ALLOWED_SPEECH_KINDS = new Set([
  "claim", "question", "answer", "propose", "agree", "object",
  "evidence", "block", "defer", "summarize", "react",
  "move_close", "ratify", "invite", "join",
]);

interface ParsedPrefix {
  kind: string | null;
  expectedResponse: { from_actor_handles: string[]; kinds?: string[] } | null;
  body: string;
}

export function parseReplyPrefix(text: string): ParsedPrefix {
  const cleaned = text.trim();
  if (!cleaned.startsWith("[")) {
    return { kind: null, expectedResponse: null, body: cleaned };
  }
  const end = cleaned.indexOf("]", 1);
  if (end === -1 || end > 200) {
    return { kind: null, expectedResponse: null, body: cleaned };
  }
  const inside = cleaned.slice(1, end).trim();
  const body = cleaned.slice(end + 1).replace(/^\s+/, "");
  const seps = ["→", "->", "@"];
  for (const sep of seps) {
    const idx = inside.indexOf(sep);
    if (idx === -1) continue;
    const kindRaw = inside.slice(0, idx).trim();
    let tail = inside.slice(idx + sep.length);
    if (sep === "@") tail = "@" + tail;
    const kind = kindRaw.toLowerCase();
    if (!ALLOWED_SPEECH_KINDS.has(kind)) {
      return { kind: null, expectedResponse: null, body: cleaned };
    }
    const { handles, kinds } = parseInviteTail(tail);
    if (handles.length === 0) {
      return { kind, expectedResponse: null, body };
    }
    const ex: { from_actor_handles: string[]; kinds?: string[] } = {
      from_actor_handles: handles,
    };
    if (kinds.length > 0) ex.kinds = kinds;
    return { kind, expectedResponse: ex, body };
  }
  // No arrow / @ — pure terminal prefix
  const kind = inside.toLowerCase();
  if (!ALLOWED_SPEECH_KINDS.has(kind)) {
    return { kind: null, expectedResponse: null, body: cleaned };
  }
  return { kind, expectedResponse: null, body };
}

function parseInviteTail(tail: string): { handles: string[]; kinds: string[] } {
  let kinds: string[] = [];
  if (tail.includes("kinds=")) {
    const idx = tail.indexOf("kinds=");
    const head = tail.slice(0, idx);
    const kindsStr = tail.slice(idx + "kinds=".length);
    tail = head;
    kinds = kindsStr.replace(/\s+/g, ",").split(",")
      .map((k) => k.trim()).filter((k) => k.length > 0);
  }
  const handles: string[] = [];
  for (const tokRaw of tail.replace(/,/g, " ").split(/\s+/)) {
    const t = tokRaw.replace(/[,;]+$/, "").trim();
    if (t.startsWith("@") && t.length > 1 && !handles.includes(t)) {
      handles.push(t);
    }
  }
  return { handles, kinds };
}
