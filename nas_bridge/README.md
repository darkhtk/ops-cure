# Ops-Cure Bridge

Ops-Cure Bridge is the control plane for Discord-driven local CLI sessions.

Responsibilities:

- runs the Discord bot and slash commands
- owns SQLite state for sessions, agents, jobs, and transcripts
- creates Discord threads and maps them to session ids
- receives outbound worker polling, registration, heartbeat, and job completion
- never executes Codex, Claude CLI, or any other local AI CLI itself

## Run locally

1. Copy `.env.example` to `.env`.
2. Set `BRIDGE_SHARED_AUTH_TOKEN` and the Discord values.
3. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

4. Start the service:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

## Docker

```bash
docker compose up --build -d
```

The SQLite database is persisted under `./data/bridge.db`.

## Synology notes

For the current target NAS, use:

- host: `semirain.synology.me`
- SSH port: `54720`
- bridge URL: `http://semirain.synology.me:18080`

`semirain.synology.me:8080` already returns a live HTTP response, so the bridge compose file publishes the service on external port `18080` to avoid collision.

Typical deployment flow:

1. Copy this `nas_bridge/` folder to the Synology NAS.
2. Edit `.env` locally on the NAS and fill `BRIDGE_SHARED_AUTH_TOKEN`, `DISCORD_BOT_TOKEN`, and optionally `DISCORD_APPLICATION_ID`.
3. Run `docker compose up -d --build` from the `nas_bridge/` directory on the NAS.
4. Confirm the bridge is reachable at `http://semirain.synology.me:18080/healthz`.

## Local development mode

Set `BRIDGE_DISABLE_DISCORD=true` to run the bridge API without connecting to Discord. In that mode, thread creation and outbound messages are logged instead of sent to Discord.
