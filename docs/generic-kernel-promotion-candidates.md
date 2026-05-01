# Generic Kernel Promotion Candidates

This document lists only the concepts that are good candidates for **later** promotion into the generic kernel.

It intentionally excludes:

- browser-first remote Codex product semantics
- Discord-specific coordination rules
- runtime adapter details
- surface UX behavior

Use this document when deciding what may eventually move from a product/service layer into the generic kernel.

Related documents:

- [generic-kernel.md](generic-kernel.md)
- [generic-kernel-product-boundary.md](generic-kernel-product-boundary.md)
- [browser-first-remote-codex.md](browser-first-remote-codex.md)

## Promotion Rule

Promote a concept into the kernel only if all of these are true:

1. at least two behaviors or products need it
2. it does not depend on browser UX wording
3. it does not depend on Discord message conventions
4. it does not depend on a specific runtime such as Codex app-server
5. it still makes sense after renaming away the current product

If any of those fail, keep it out of the kernel.

## Candidate 1: Presence and Lease

This is the strongest promotion candidate.

Status:

- a minimal generic `ActorSession` / `ResourceLease` primitive is now implemented in the kernel
- orchestration worker/job lifecycle and browser-first remote task flow should consume the same primitive
- browser-first remote Codex should consume that primitive rather than promoting `RemoteTask` directly into the kernel
- higher-level operation/decision/evidence semantics still remain outside the kernel for now

### Why

The same pattern already appears in:

- chat participant ownership
- orchestration worker claim
- remote task ownership
- future ops ownership

### Generic Responsibility

- who is present in a scope
- who currently holds a resource
- when the claim expires
- whether the claim is stale

### Draft Models

```text
ActorSession
- session_id
- actor_id
- scope_kind
- scope_id
- status
- last_seen_at
- expires_at

ResourceLease
- lease_id
- resource_kind
- resource_id
- holder_actor_id
- lease_token
- claimed_at
- expires_at
- status
```

### Candidate API Shape

```text
POST /api/leases
POST /api/leases/{lease_id}/heartbeat
POST /api/leases/{lease_id}/release
GET /api/scopes/{scope_id}/presence
```

### Keep Out of Kernel

Do not include:

- machine picker logic
- Discord mention ownership rules
- browser queue wording

## Candidate 2: Generic Operation

This is the long-term abstraction behind product-level `RemoteTask`.

Status:

- ✅ **promoted (PR8)**: `kernel/operations.py` now defines the
  SQLAlchemy `OperationModel` family (5 child tables for
  Assignment / Heartbeat / Evidence / Approval / Note). The
  product-level `RemoteTask*` names continue to work as Python
  aliases in `app.models` so existing call sites and tests are
  untouched. Underlying tables and column shapes are unchanged.
- ✅ `kernel/operation_service.py` exposes `KernelOperationService`
  as the canonical kernel-vocabulary entry point. The implementation
  body is still in `app/services/remote_task_service.py`; a follow-up
  PR can move it once call sites have migrated.
- 🔜 next promotion step: collapse the `(machine_id, thread_id)`
  scope pair into a single `space_id` column to remove sentinels
  like `machine_id="chat"`. Deferred because it requires a DB
  column rename and coordinated caller changes.
- 🔜 schema unification: the Pydantic shape sketches in
  `kernel/operations.py` (`OperationSummary` and friends) are still
  decoupled from the wire. Aligning them with
  `RemoteTaskSummaryResponse` is a separate API-ergonomics PR.

### Why

The ownership/progress/evidence lifecycle is broader than one product.

It can model:

- remote Codex work
- orchestration jobs
- verification jobs
- ops remediation steps

### Generic Responsibility

- represent one unit of work
- allow assignment
- track lifecycle state
- track progress heartbeat
- attach evidence

### Draft Models

```text
Operation
- operation_id
- space_id
- subject_kind
- subject_id
- kind
- objective
- requested_by
- status
- created_at
- updated_at

OperationAssignment
- operation_id
- actor_id
- lease_id
- status
- claimed_at
- released_at

OperationHeartbeat
- operation_id
- actor_id
- phase
- summary
- metrics_json
- created_at

OperationEvidence
- operation_id
- actor_id
- kind
- summary
- payload_json
- created_at
```

### Suggested Generic Status Set

```text
queued
claimed
executing
verifying
blocked
interrupted
completed
failed
stalled
```

### Candidate API Shape

```text
POST /api/operations
GET /api/operations/{operation_id}
POST /api/operations/{operation_id}/claim
POST /api/operations/{operation_id}/heartbeat
POST /api/operations/{operation_id}/evidence
POST /api/operations/{operation_id}/complete
POST /api/operations/{operation_id}/fail
POST /api/operations/{operation_id}/interrupt
```

### Keep Out of Kernel

Do not include:

- Codex thread-specific wording
- browser task panel grouping
- remote composer behavior
- product rules like "evidence required before executing" unless reused elsewhere

## Candidate 3: Decision Request

Approval is product-specific today, but the underlying shape is generic.

### Why

More than one behavior may need:

- human approval
- confirmation
- policy gate
- explicit resolution

### Generic Responsibility

- represent a pending decision attached to an operation
- track request and resolution state

### Draft Model

```text
DecisionRequest
- decision_id
- operation_id
- kind
- status
- reason
- note
- requested_by
- requested_at
- resolved_by
- resolution
- resolved_at
```

### Candidate API Shape

```text
GET /api/operations/{operation_id}/decision
POST /api/operations/{operation_id}/decision
POST /api/operations/{operation_id}/decision/resolve
```

### Keep Out of Kernel

Do not include:

- Codex-specific approval copy
- browser approve/reject button layout
- runtime-specific approval triggers

## Candidate 4: Evidence and Artifact References

This is a good promotion target once evidence is reused outside one product.

### Why

Structured proof of work is useful across:

- remote Codex execution
- orchestration verification
- ops remediation

### Generic Responsibility

- attach evidence to an operation
- store references to generated artifacts
- expose machine-readable metrics

### Draft Models

```text
ArtifactRef
- artifact_id
- operation_id
- kind
- label
- uri
- metadata_json

EvidenceMetric
- commands_run
- files_read
- files_modified
- tests_run
```

### Candidate API Shape

```text
GET /api/operations/{operation_id}/evidence
POST /api/operations/{operation_id}/evidence
GET /api/operations/{operation_id}/artifacts
```

### Keep Out of Kernel

Do not include:

- browser evidence badge presentation
- product-specific thresholds for "real work started"
- Codex runtime event names

## Candidate 5: Stream Metadata Helpers

The kernel already owns event streaming. A small amount of reusable stream metadata may be promoted as needed.

### Why

Reconnect, replay, and freshness are cross-cutting concerns.

### Generic Responsibility

- cursor resume
- replay/reset semantics
- subscription freshness metadata

### Draft Additions

```text
StreamState
- accepted_after_cursor
- latest_cursor
- freshness
- reset_reason
```

### Keep Out of Kernel

Do not include:

- transcript-specific UX labels
- browser copy like "Reconnecting" or "Synced"
- product-specific stall heuristics

## Recommended Promotion Order

Promote in this order only when reuse is proven:

1. `Presence / Lease`
2. `Operation`
3. `DecisionRequest`
4. `Evidence / ArtifactRef`
5. small `StreamState` helpers

This order keeps the kernel from freezing around one product too early.

## Recommended Non-Promotions

The following should stay out of the kernel even long-term:

- `RemoteTask` as a browser/Codex-specific name
- machine/thread browser UX
- Discord note formats and tags
- runtime adapter logic
- Codex transcript semantics

## Summary

If something is only needed to make browser-first remote Codex feel good, it is not a kernel candidate.

The later-promotion candidates are only:

- lease/presence
- operation lifecycle
- decision gate
- evidence/artifact refs
- limited stream metadata

Everything else should remain in the product/service layer until proven reusable.
