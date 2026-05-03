/**
 * Entry point. Wires:
 *
 *   config (env)  →  BridgeClient  →  SSE consumer  →  Agent dispatch
 *                              ↘   heartbeat ticker
 *
 * Reconnect: SSE errors trigger exponential backoff with jitter,
 * matching agent_loop.py's strategy (spec §5 / §13 imply nothing
 * but agent_loop.py implements it; we replicate behavior so multi-
 * agent reconnect storms behave the same regardless of client lang).
 */

import { loadConfig } from "./config.ts";
import { BridgeClient } from "./http.ts";
import { Agent } from "./agent.ts";
import type { InboxEnvelope } from "./types.ts";

const SSE_RECONNECT_BASE_MS = 1000;
const SSE_RECONNECT_MAX_MS = 60_000;

function log(msg: string): void {
  // single-line stderr for parity with agent_loop's logging shape
  process.stderr.write(`[ts-agent-loop] ${msg}\n`);
}

async function startHeartbeat(
  client: BridgeClient,
  handle: string,
  intervalSec: number,
  signal: AbortSignal,
): Promise<void> {
  const path = `/v2/actors/${encodeURIComponent(handle.replace(/^@/, ""))}/heartbeat`;
  // First heartbeat fires after one interval, not at startup —
  // SSE subscribe already proves liveness for the bridge.
  while (!signal.aborted) {
    try {
      await new Promise<void>((resolve, reject) => {
        const t = setTimeout(resolve, intervalSec * 1000);
        signal.addEventListener("abort", () => {
          clearTimeout(t);
          reject(new Error("aborted"));
        }, { once: true });
      });
    } catch {
      return;
    }
    try {
      await client.postJson(path, {});
    } catch (e) {
      log(`heartbeat error: ${(e as Error).message}`);
    }
  }
}

async function consumeSSE(
  client: BridgeClient,
  agent: Agent,
  handle: string,
  signal: AbortSignal,
): Promise<void> {
  const path = `/v2/inbox/stream?actor_handle=${encodeURIComponent(handle)}`;
  let attempt = 0;
  while (!signal.aborted) {
    try {
      log(`connecting SSE: ${path}`);
      for await (const frame of client.streamSSE(path, signal)) {
        attempt = 0; // reset on first delivered frame
        if (frame.event === "open") {
          try {
            const open = JSON.parse(frame.data) as { actor_id?: string };
            if (open.actor_id) {
              agent.setActorId(open.actor_id);
              log(`subscribed: actor_id=${open.actor_id}`);
            }
          } catch (e) {
            log(`bad open frame: ${(e as Error).message}`);
          }
          continue;
        }
        if (frame.event !== "v2.event") continue;
        let env: InboxEnvelope;
        try {
          env = JSON.parse(frame.data) as InboxEnvelope;
        } catch (e) {
          log(`bad envelope: ${(e as Error).message}`);
          continue;
        }
        if (!agent.markSeen(env.event_id)) continue;
        if (!agent.shouldRespond(env)) continue;
        const reply = agent.buildReply(env);
        if (!reply) continue;
        try {
          await agent.post(env, reply);
          log(`replied to op=${env.operation_id} kind=${reply.kind} len=${reply.text.length}`);
        } catch (e) {
          log(`reply post failed op=${env.operation_id}: ${(e as Error).message}`);
        }
      }
    } catch (e) {
      if (signal.aborted) return;
      attempt += 1;
      const base = Math.min(
        SSE_RECONNECT_BASE_MS * Math.pow(2, Math.max(0, attempt - 1)),
        SSE_RECONNECT_MAX_MS,
      );
      const jitter = base * (Math.random() * 0.2);
      const wait = base + jitter;
      log(`SSE error: ${(e as Error).message}; reconnect attempt=${attempt} in ${(wait / 1000).toFixed(1)}s`);
      await new Promise((r) => setTimeout(r, wait));
    }
  }
}

async function main(): Promise<void> {
  const cfg = loadConfig();
  const client = new BridgeClient(cfg);
  const agent = new Agent(cfg, client);
  const ac = new AbortController();
  const onStop = () => {
    log("shutting down");
    ac.abort();
  };
  process.on("SIGINT", onStop);
  process.on("SIGTERM", onStop);
  log(`starting handle=${cfg.actorHandle} bridge=${cfg.bridgeUrl} version=${cfg.protocolVersion}`);
  await Promise.all([
    consumeSSE(client, agent, cfg.actorHandle, ac.signal),
    startHeartbeat(client, cfg.actorHandle, cfg.heartbeatIntervalSeconds, ac.signal),
  ]);
}

main().catch((e: Error) => {
  process.stderr.write(`fatal: ${e.message}\n${e.stack ?? ""}\n`);
  process.exit(1);
});
