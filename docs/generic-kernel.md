# Opscure Generic Kernel Split

Opscure is moving from a project-specific orchestration bridge toward a generic channel-based event/state kernel.

## Current 1st-phase split

- `nas_bridge/app/kernel`
  - Common runtime primitives such as event log, drift tracking, registry, and storage helpers.
- `nas_bridge/app/behaviors/orchestration`
  - The public Discord planner/curator/coder/reviewer/verifier orchestration behavior package.
- `nas_bridge/app/behaviors/workflow`
  - The legacy internal implementation package that now backs the public orchestration behavior.
- `nas_bridge/app/behaviors/chat`
  - A Codex-to-Codex dialogue behavior with its own Discord commands, message handler, and persistence model.
- `nas_bridge/app/behaviors/ops`
  - A lightweight incident/operations room behavior with issue and resolve events.
- `nas_bridge/app/behaviors/game`
  - Placeholder behavior for future room/game-loop style spaces.
- `nas_bridge/app/transports/discord`
  - Discord transport wiring, including command registration and inbound message routing contracts.
- `nas_bridge/app/presenters/discord`
  - Discord rendering such as status cards and embed-oriented presentation.
- `pc_launcher/runtimes/local_windows`
  - The current local Windows launcher/worker runtime.
- `pc_launcher/domains/workflow_default`
  - The current sample workflow domain bundle.

## What changed in this phase

- The bridge entrypoints now import orchestration, kernel, transport, and presenter pieces from the new package layout.
- Discord command definitions were moved under orchestration behavior and registered through transport-level providers.
- Discord message handling was moved under orchestration behavior and wired through transport-level handler contracts.
- A non-workflow `chat` behavior was added to validate that Discord transport can host behavior plugins without task/handoff semantics.
- A second non-workflow `ops` behavior was added to validate that the kernel can support room-style coordination without workflow semantics.
- A kernel-level `Space` vocabulary was added so orchestration sessions, chat threads, and ops rooms can be queried through the same generic summary shape.
- Kernel-level `Actor` and `Event` vocabularies were added so orchestration, chat, and ops spaces can expose participants and recent activity through the same generic API shape.
- `Space`, `Actor`, and `Event` services now resolve through behavior-owned kernel providers instead of importing orchestration/chat/ops models directly.
- A behavior catalog layer now exposes registered behaviors and their capabilities, including Discord command/message support and kernel `Space/Actor/Event` support.
- Old top-level modules such as `command_router.py` and `message_router.py` remain as compatibility shims.

## What is still orchestration-specific

- `SessionService`
- Task, handoff, and verification semantics
- Slash commands like `/project start`
- Markdown workflow projections such as `CURRENT_STATE.md`

These still belong to the orchestration behavior and are not part of the generic kernel.

## Next refactor targets

1. Pull more orchestration-specific persistence out of legacy top-level models and into behavior-owned model modules.
2. Extend the kernel vocabulary beyond `Space / Actor / Event` into generic `Snapshot / Timer / Rule` services.
3. Add another non-workflow behavior, such as a room/game-loop behavior, to validate the plugin architecture further.
4. Make behavior loading configurable so a deployment can enable only the behaviors it needs.
