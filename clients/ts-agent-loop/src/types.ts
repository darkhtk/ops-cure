/**
 * v3 wire types — hand-typed from docs/protocol-v3-spec.md §6.
 *
 * Deliberately NOT generated from OpenAPI: spec ambiguity is the
 * thing this client is supposed to surface, and codegen would mask
 * it. Each field carries a comment noting which spec § defines it.
 */

/** Spec §6.2 */
export interface ExpectedResponse {
  from_actor_handles: string[];
  kinds?: string[];        // includes the literal "*" sentinel for "any"
  by_round_seq?: number;
}

/** Spec §6.1 */
export interface OperationPolicy {
  close_policy: string;
  join_policy: string;
  context_compaction: string;
  max_rounds: number | null;
  min_ratifiers: number | null;
  bot_open: boolean;
}

/** Spec §6.4 */
export interface OperationEvent {
  id: string;
  operation_id: string;
  actor_id: string;
  seq: number;
  kind: string;
  payload: { text?: string } & Record<string, unknown>;
  addressed_to_actor_ids: string[];
  private_to_actor_ids: string[] | null;
  replies_to_event_id: string | null;
  expected_response: ExpectedResponse | null;
  created_at: string;
}

/**
 * Wire envelope delivered on `/v2/inbox/stream`. Note this is the
 * *SSE wrapping* of the event log -- ``event_id`` corresponds to
 * OperationEvent.id, ``actor_id`` is the speaker, etc.
 */
export interface InboxEnvelope {
  operation_id: string;
  event_id: string;
  seq: number;
  kind: string;
  actor_id: string;
  payload: { text?: string } & Record<string, unknown>;
  addressed_to_actor_ids: string[];
  private_to_actor_ids: string[] | null;
  replies_to_event_id: string | null;
  expected_response: ExpectedResponse | null;
  created_at: string;
  cursor: string;
}
