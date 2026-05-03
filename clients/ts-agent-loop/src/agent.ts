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
   *  meaningful to say (the WIRE-level "agent had nothing to add"). */
  buildReply(env: InboxEnvelope): { kind: string; text: string } | null {
    const triggerText = (env.payload.text ?? "").toString().slice(0, 200);
    if (env.kind === "chat.speech.question") {
      return {
        kind: "speech.answer",
        text: `Acknowledged via ts-agent-loop: ${triggerText}`,
      };
    }
    // For any other kind we deliberately skip; the agent is a
    // *validator of the wire*, not a chatty participant.
    return null;
  }

  async post(env: InboxEnvelope, reply: { kind: string; text: string }): Promise<void> {
    await this.client.postJson(
      `/v2/operations/${env.operation_id}/events`,
      {
        actor_handle: this.cfg.actorHandle,
        kind: reply.kind,
        payload: { text: reply.text },
        replies_to_event_id: env.event_id,
      },
    );
  }
}
