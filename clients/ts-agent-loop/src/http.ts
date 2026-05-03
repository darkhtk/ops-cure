/**
 * HTTP client — fetch wrapper that injects auth + traceparent +
 * X-Protocol-Version on every request.
 *
 * Uses Node 24's built-in fetch + AbortController. No third-party
 * HTTP lib so we can attribute any wire weirdness to the spec, not
 * to a library quirk.
 */

import type { AgentConfig } from "./config.ts";

export interface AuthHeaders {
  Authorization: string;
  "X-Protocol-Version": string;
  "X-Actor-Token"?: string;
  traceparent?: string;
}

export class BridgeClient {
  private readonly base: string;
  private readonly cfg: AgentConfig;
  private currentTraceparent: string | null = null;

  constructor(cfg: AgentConfig) {
    this.cfg = cfg;
    this.base = cfg.bridgeUrl;
  }

  /** SSE captured this; subsequent POSTs / heartbeats reuse it. */
  setTraceparent(value: string | null): void {
    this.currentTraceparent = value;
  }

  authHeaders(extra: Record<string, string> = {}): Record<string, string> {
    const h: Record<string, string> = {
      Authorization: `Bearer ${this.cfg.sharedToken}`,
      "X-Protocol-Version": this.cfg.protocolVersion,
      ...extra,
    };
    if (this.cfg.actorToken) h["X-Actor-Token"] = this.cfg.actorToken;
    if (this.currentTraceparent) h["traceparent"] = this.currentTraceparent;
    return h;
  }

  async getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
    const opts: RequestInit = { method: "GET", headers: this.authHeaders() };
    if (signal !== undefined) opts.signal = signal;
    const r = await fetch(`${this.base}${path}`, opts);
    if (!r.ok) throw new Error(`GET ${path} ${r.status}: ${await r.text()}`);
    return await r.json() as T;
  }

  async postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    const opts: RequestInit = {
      method: "POST",
      headers: this.authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    };
    if (signal !== undefined) opts.signal = signal;
    const r = await fetch(`${this.base}${path}`, opts);
    if (!r.ok) {
      throw new Error(`POST ${path} ${r.status}: ${await r.text()}`);
    }
    return await r.json() as T;
  }

  /** SSE consumer. Yields parsed { event, data } records as they arrive.
   *  Captures the response's traceparent header into setTraceparent so
   *  subsequent POSTs in this session inherit the same trace_id. */
  async *streamSSE(
    path: string,
    signal: AbortSignal,
  ): AsyncGenerator<{ event: string; data: string }, void, void> {
    const r = await fetch(`${this.base}${path}`, {
      method: "GET",
      headers: { ...this.authHeaders(), Accept: "text/event-stream" },
      signal,
    });
    if (!r.ok) {
      throw new Error(`SSE ${path} ${r.status}: ${await r.text()}`);
    }
    const tp = r.headers.get("traceparent");
    if (tp) this.setTraceparent(tp);
    if (!r.body) throw new Error("SSE response has no body");

    const reader = r.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let event = "";
    const dataLines: string[] = [];

    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        // Parse line by line. SSE separator is "\n" (or "\r\n");
        // an empty line terminates the current event.
        let nl: number;
        while ((nl = buffer.indexOf("\n")) !== -1) {
          let line = buffer.slice(0, nl);
          buffer = buffer.slice(nl + 1);
          if (line.endsWith("\r")) line = line.slice(0, -1);
          if (line === "") {
            if (event && dataLines.length > 0) {
              yield { event, data: dataLines.join("\n") };
            }
            event = "";
            dataLines.length = 0;
            continue;
          }
          if (line.startsWith(":")) continue; // SSE comment
          if (line.startsWith("event:")) {
            event = line.slice("event:".length).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice("data:".length).replace(/^ /, ""));
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
}
