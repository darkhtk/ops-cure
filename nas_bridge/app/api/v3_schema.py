"""v3 protocol schema discovery.

The bridge auto-generates a FastAPI/OpenAPI doc, but most of the
surface there is internal (remote_codex agents, /api/* lifecycle,
discord gateway internals). External implementers want a clean
canonical view of the v3 protocol *only*. This module exposes:

  GET /v3/schema/types              hand-curated JSON Schemas for the
                                    canonical request / payload types
                                    (OperationPolicy, ExpectedResponse,
                                    speech kinds, error codes).

  GET /v3/schema/openapi-public     OpenAPI 3.1 filtered to endpoints
                                    tagged ``protocol-v3-public``. The
                                    full FastAPI OpenAPI is still at
                                    /openapi.json for in-house tooling;
                                    this is the externally-stable shape.

Discovery itself does NOT require auth — implementers should be able
to read schemas before negotiating identity. Mutating endpoints stay
behind the existing bearer.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.openapi.utils import get_openapi

from ..kernel.v2 import contract as _v2_contract

router = APIRouter(prefix="/v3/schema", tags=["protocol-v3-public"])


_PUBLIC_TAG = "protocol-v3-public"


def _operation_policy_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://opscure.local/v3/schema/OperationPolicy",
        "title": "OperationPolicy",
        "description": (
            "Per-op governance policy. Materialized at op-open time; "
            "missing fields fall back to DEFAULT_OPERATION_POLICY."
        ),
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "close_policy": {
                "type": "string",
                "enum": sorted(_v2_contract.ALL_CLOSE_POLICIES),
                "description": (
                    "Who may close the op. ``opener_unilateral`` = "
                    "only the opener. ``any_participant`` = any "
                    "participant. ``operator_ratifies`` = an actor with "
                    "role=operator must post chat.speech.ratify before "
                    "close. ``quorum`` = N distinct ratifiers (see "
                    "min_ratifiers)."
                ),
            },
            "join_policy": {
                "type": "string",
                "enum": sorted(_v2_contract.ALL_JOIN_POLICIES),
                "description": (
                    "Membership admission rule. ``open`` = anyone may "
                    "join. ``self_or_invite`` = self-join allowed (default). "
                    "``invite_only`` = a prior speech.invite from an "
                    "existing participant required."
                ),
            },
            "context_compaction": {
                "type": "string",
                "enum": sorted(_v2_contract.ALL_CONTEXT_COMPACTIONS),
                "description": (
                    "How the bridge handles transcript growth. "
                    "``rolling_summary`` requires an external summarizer "
                    "agent and is not enforced in-bridge."
                ),
            },
            "max_rounds": {
                "type": ["integer", "null"],
                "minimum": 1,
                "description": (
                    "Op-level cap on chat.speech.* events. The "
                    "(N+1)-th submission is rejected with HTTP 400 + "
                    "code ``policy.max_rounds_exhausted``."
                ),
            },
            "min_ratifiers": {
                "type": ["integer", "null"],
                "minimum": 1,
                "description": (
                    "Required when close_policy=quorum. Number of "
                    "distinct actors whose chat.speech.ratify events "
                    "must be present before close is admissible."
                ),
            },
            "bot_open": {
                "type": "boolean",
                "description": (
                    "When false, only actors with kind=human may open "
                    "ops with this policy. Default true."
                ),
            },
        },
        "default": _v2_contract.DEFAULT_OPERATION_POLICY,
    }


def _expected_response_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://opscure.local/v3/schema/ExpectedResponse",
        "title": "ExpectedResponse",
        "description": (
            "Reply contract attached to a speech event. The bridge "
            "fans the event to the listed actors and the policy "
            "engine validates reply kinds + by_round_seq expiry."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": ["from_actor_handles"],
        "properties": {
            "from_actor_handles": {
                "type": "array",
                "items": {"type": "string", "pattern": "^@.+"},
                "minItems": 0,
                "description": (
                    "Handles obligated to reply. The bridge normalizes "
                    "missing '@' prefix on write."
                ),
            },
            "kinds": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [_v2_contract.EXPECTED_RESPONSE_KIND_WILDCARD]
                            + sorted(_v2_contract.SPEECH_KINDS),
                },
                "description": (
                    "Whitelist of valid reply speech kinds. ``*`` "
                    "means any kind. ``defer`` is universally "
                    "admissible and need not be listed."
                ),
            },
            "by_round_seq": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Op event seq by which a qualifying reply must "
                    "have arrived. After the op MAX(seq) exceeds this "
                    "value, the policy_sweeper auto-emits a "
                    "chat.speech.defer on the addressee's behalf."
                ),
            },
        },
    }


def _speech_kinds_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://opscure.local/v3/schema/SpeechKinds",
        "title": "SpeechKinds",
        "description": "Closed set of speech kinds the bridge accepts.",
        "type": "string",
        "enum": sorted(_v2_contract.SPEECH_KINDS),
    }


def _error_codes_schema() -> dict[str, Any]:
    """Stable wire-contract error codes from the policy engine.
    Clients should match on these strings to branch behavior."""
    from ..kernel.v2 import (
        CODE_MAX_ROUNDS_EXHAUSTED, CODE_REPLY_KIND_REJECTED,
        CODE_CLOSE_NEEDS_OPERATOR, CODE_CLOSE_NEEDS_QUORUM,
        CODE_CLOSE_NEEDS_PARTICIPANT, CODE_JOIN_INVITE_ONLY,
        CODE_INVITE_NEEDS_PARTICIPANT,
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://opscure.local/v3/schema/ErrorCodes",
        "title": "PolicyErrorCodes",
        "description": (
            "Stable error code strings returned (in HTTP 400/403 "
            "responses) when a policy gate fires. Each code has a "
            "single semantic meaning across versions."
        ),
        "type": "string",
        "enum": [
            CODE_MAX_ROUNDS_EXHAUSTED,
            CODE_REPLY_KIND_REJECTED,
            CODE_CLOSE_NEEDS_OPERATOR,
            CODE_CLOSE_NEEDS_QUORUM,
            CODE_CLOSE_NEEDS_PARTICIPANT,
            CODE_JOIN_INVITE_ONLY,
            CODE_INVITE_NEEDS_PARTICIPANT,
        ],
    }


@router.get("/types")
def get_schema_types() -> dict[str, Any]:
    """Return the canonical v3 type schemas. Hand-curated; pydantic
    autoschema does not capture protocol-level constraints (enum
    vocabulary, default policy, etc.)."""
    return {
        "version": "3.x",
        "schemas": {
            "OperationPolicy": _operation_policy_schema(),
            "ExpectedResponse": _expected_response_schema(),
            "SpeechKinds": _speech_kinds_schema(),
            "PolicyErrorCodes": _error_codes_schema(),
        },
    }


@router.get("/openapi-public")
def get_public_openapi(request: Request) -> dict[str, Any]:
    """Return an OpenAPI doc filtered to ``protocol-v3-public``-tagged
    endpoints. The full FastAPI OpenAPI (including internal endpoints
    like /api/remote-claude/*) lives at /openapi.json; that surface is
    NOT a stable contract."""
    full = get_openapi(
        title="Opscure Bridge — v3 public protocol",
        version="3.x",
        description=(
            "External v3 protocol surface. Internal endpoints (remote "
            "claude/codex agents, lifecycle hooks) are filtered out."
        ),
        routes=request.app.routes,
    )
    paths_filtered: dict[str, Any] = {}
    for path, ops in (full.get("paths") or {}).items():
        kept_ops = {
            method: spec for method, spec in ops.items()
            if isinstance(spec, dict) and _PUBLIC_TAG in (spec.get("tags") or [])
        }
        if kept_ops:
            paths_filtered[path] = kept_ops
    full["paths"] = paths_filtered
    return full
