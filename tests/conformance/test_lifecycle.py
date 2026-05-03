"""Conformance: op lifecycle + event log. Spec §7.1, §6.4."""
import pytest


@pytest.mark.conformance_required
def test_open_op_returns_normalized_policy(client, space_id):
    """Spec §6.1: every op MUST have a materialized policy at open."""
    r = client.post(
        "/v2/operations",
        json={
            "space_id": space_id, "kind": "inquiry",
            "title": "policy norm test", "opener_actor_handle": "@alice",
        },
    )
    assert r.status_code == 201
    op = r.json()
    assert "policy" in op
    p = op["policy"]
    for field in (
        "close_policy", "join_policy", "context_compaction",
        "max_rounds", "min_ratifiers", "bot_open",
    ):
        assert field in p, f"policy missing {field}"


@pytest.mark.conformance_required
def test_event_log_seq_monotonic(client, space_id):
    r = client.post(
        "/v2/operations",
        json={
            "space_id": space_id, "kind": "inquiry",
            "title": "seq test", "opener_actor_handle": "@alice",
        },
    )
    op_id = r.json()["id"]
    # Post a few events
    for i in range(3):
        client.post(
            f"/v2/operations/{op_id}/events",
            json={
                "actor_handle": "@alice",
                "kind": "speech.claim",
                "payload": {"text": f"speech {i}"},
            },
        )
    r = client.get(
        f"/v2/operations/{op_id}/events",
        params={"actor_handle": "@alice"},
    )
    seqs = [e["seq"] for e in r.json()["events"]]
    assert seqs == sorted(seqs), f"event seq not monotonic: {seqs}"
    assert seqs == list(range(1, len(seqs) + 1)), f"seq has gaps: {seqs}"


@pytest.mark.conformance_required
def test_replies_to_event_id_persists_pre_fanout(client, space_id):
    """Spec §6.4: replies_to is set at write time, not post-stamped."""
    r = client.post(
        "/v2/operations",
        json={
            "space_id": space_id, "kind": "inquiry",
            "title": "reply chain", "opener_actor_handle": "@alice",
        },
    )
    op_id = r.json()["id"]
    parent = client.post(
        f"/v2/operations/{op_id}/events",
        json={
            "actor_handle": "@alice", "kind": "speech.question",
            "payload": {"text": "?"},
        },
    ).json()
    child = client.post(
        f"/v2/operations/{op_id}/events",
        json={
            "actor_handle": "@bob", "kind": "speech.answer",
            "payload": {"text": "!"},
            "replies_to_event_id": parent["id"],
        },
    ).json()
    assert child["replies_to_event_id"] == parent["id"]


@pytest.mark.conformance_required
def test_private_event_redacted_from_history(client, space_id):
    """Spec §10.1: private_to_actors enforced on GET /events."""
    r = client.post(
        "/v2/operations",
        json={
            "space_id": space_id, "kind": "inquiry",
            "title": "privacy test",
            "opener_actor_handle": "@alice",
        },
    )
    op_id = r.json()["id"]
    # alice + bob whisper
    client.post(
        f"/v2/operations/{op_id}/events",
        json={
            "actor_handle": "@alice", "kind": "speech.claim",
            "payload": {"text": "WHISPER for bob only"},
            "addressed_to": "bob",
            "private_to_actors": ["bob"],
        },
    )
    # carol joins
    client.post(
        f"/v2/operations/{op_id}/events",
        json={
            "actor_handle": "@carol", "kind": "speech.join",
            "payload": {"text": "joining"},
        },
    )
    # carol fetches history → must NOT see whisper
    r = client.get(
        f"/v2/operations/{op_id}/events",
        params={"actor_handle": "@carol"},
    )
    texts = [
        (e.get("payload") or {}).get("text", "") for e in r.json()["events"]
    ]
    assert not any("WHISPER" in t for t in texts), (
        f"privacy leak in carol's view: {texts}"
    )
    # bob fetches history → MUST see it
    r = client.get(
        f"/v2/operations/{op_id}/events",
        params={"actor_handle": "@bob"},
    )
    texts = [
        (e.get("payload") or {}).get("text", "") for e in r.json()["events"]
    ]
    assert any("WHISPER" in t for t in texts), (
        f"bob did not see his whisper: {texts}"
    )
