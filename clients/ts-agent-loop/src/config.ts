/**
 * Config — environment → strongly-typed Config struct.
 *
 * Mirrors pc_launcher/connectors/claude_executor/runner.py env vars
 * exactly. Mismatch here is the first interop signal: anything we
 * picked up implicitly in Python should come from a concrete env
 * field with a concrete validation rule.
 */

export interface AgentConfig {
  bridgeUrl: string;
  sharedToken: string;
  actorHandle: string;
  actorToken: string | null;
  systemPrompt: string;
  heartbeatIntervalSeconds: number;
  protocolVersion: string;
}

function envOrThrow(key: string): string {
  const v = process.env[key]?.trim();
  if (!v) throw new Error(`required env var missing: ${key}`);
  return v;
}

function envOrDefault(key: string, fallback: string): string {
  return process.env[key]?.trim() || fallback;
}

export function loadConfig(): AgentConfig {
  const handle = envOrThrow("CLAUDE_BRIDGE_ACTOR_HANDLE");
  return {
    bridgeUrl: envOrThrow("CLAUDE_BRIDGE_URL").replace(/\/+$/, ""),
    sharedToken: envOrThrow("CLAUDE_BRIDGE_TOKEN"),
    actorHandle: handle.startsWith("@") ? handle : `@${handle}`,
    actorToken: process.env["CLAUDE_BRIDGE_AGENT_ACTOR_TOKEN"]?.trim() || null,
    systemPrompt: envOrDefault("CLAUDE_BRIDGE_AGENT_SYSTEM_PROMPT", ""),
    heartbeatIntervalSeconds: Number(
      envOrDefault("CLAUDE_BRIDGE_AGENT_HEARTBEAT_SECONDS", "60"),
    ),
    protocolVersion: envOrDefault("CLAUDE_BRIDGE_PROTOCOL_VERSION", "3.1"),
  };
}
