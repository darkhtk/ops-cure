"""Conformance: policy enforcement. Spec §12."""
import pytest


def _open_op(client, *, space_id, title, policy=None, opener="@alice", addressed_to=None):
    body = {
        "space_id": space_id, "kind": "inquiry",
        "title": title, "opener_actor_handle": opener,
    }
    if policy is not None:
        body["policy"] = policy
    if addressed_to is not None:
        body["addressed_to"] = addressed_to
    r = client.post("/v2/operations", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _say(client, op_id, *, actor, kind, text, **kwargs):
    payload = {"text": text}
    # D9 / rev-9: ratify only counts toward quorum when carrying
    # close-intent. Conformance suite uses ratify exclusively for
    # quorum voting, so stamp intent=close automatically.
    if kind == "ratify":
        payload["intent"] = "close"
    body = {
        "actor_handle": actor,
        "kind": f"speech.{kind}",
        "payload": payload,
    }
    body.update(kwargs)
    return client.post(f"/v2/operations/{op_id}/events", json=body)


@pytest.mark.conformance_required
def test_max_rounds_rejects_past_cap(client, space_id):
    op_id = _open_op(client, space_id=space_id, title="cap test",
                     policy={"max_rounds": 2})
    r = _say(client, op_id, actor="@alice", kind="claim", text="1")
    assert r.status_code == 201
    r = _say(client, op_id, actor="@alice", kind="claim", text="2")
    assert r.status_code == 201
    r = _say(client, op_id, actor="@alice", kind="claim", text="3")
    assert r.status_code == 400
    assert "max_rounds" in r.text.lower()


@pytest.mark.conformance_required
def test_reply_kind_whitelist_rejects_off_list(client, space_id):
    op_id = _open_op(client, space_id=space_id, title="kind test")
    r = _say(
        client, op_id,
        actor="@alice", kind="question", text="?",
        expected_response={
            "from_actor_handles": ["@bob"],
            "kinds": ["answer"],
        },
    )
    assert r.status_code == 201
    trigger_id = r.json()["id"]
    # claim is NOT in whitelist
    r = _say(
        client, op_id,
        actor="@bob", kind="claim", text="DNS",
        replies_to_event_id=trigger_id,
    )
    assert r.status_code == 400
    assert "kind" in r.text.lower()
    # answer IS in whitelist
    r = _say(
        client, op_id,
        actor="@bob", kind="answer", text="checking",
        replies_to_event_id=trigger_id,
    )
    assert r.status_code == 201


@pytest.mark.conformance_required
def test_defer_universally_admissible(client, space_id):
    """``defer`` is allowed regardless of expected_response.kinds —
    spec §6.2 carve-out so the auto-defer sweeper can do its job."""
    op_id = _open_op(client, space_id=space_id, title="defer carve")
    r = _say(
        client, op_id,
        actor="@alice", kind="question", text="?",
        expected_response={
            "from_actor_handles": ["@bob"],
            "kinds": ["answer"],   # explicitly excludes defer
        },
    )
    trigger_id = r.json()["id"]
    r = _say(
        client, op_id,
        actor="@bob", kind="defer", text="cannot answer",
        replies_to_event_id=trigger_id,
    )
    assert r.status_code == 201


@pytest.mark.conformance_required
def test_close_quorum_blocks_until_min_ratifiers_distinct(client, space_id):
    op_id = _open_op(
        client, space_id=space_id, title="quorum test",
        policy={"close_policy": "quorum", "min_ratifiers": 2},
    )
    # 0 ratifiers → blocked
    r = client.post(
        f"/v2/operations/{op_id}/close",
        json={"actor_handle": "@alice", "resolution": "answered"},
    )
    assert r.status_code == 400
    # bob ratifies twice → still 1 distinct
    _say(client, op_id, actor="@bob", kind="ratify", text="ok")
    _say(client, op_id, actor="@bob", kind="ratify", text="still ok")
    r = client.post(
        f"/v2/operations/{op_id}/close",
        json={"actor_handle": "@alice", "resolution": "answered"},
    )
    assert r.status_code == 400
    # carol ratifies → 2 distinct → close passes
    _say(client, op_id, actor="@carol", kind="ratify", text="seconded")
    r = client.post(
        f"/v2/operations/{op_id}/close",
        json={"actor_handle": "@alice", "resolution": "answered"},
    )
    assert r.status_code == 200


@pytest.mark.conformance_required
def test_join_invite_only_blocks_uninvited_self_join(client, space_id):
    op_id = _open_op(
        client, space_id=space_id, title="invite-only test",
        policy={"join_policy": "invite_only"},
    )
    r = _say(client, op_id, actor="@stranger", kind="join", text="hi")
    assert r.status_code == 400
    assert "invite" in r.text.lower()


@pytest.mark.conformance_required
def test_default_policy_close_succeeds(client, space_id):
    """Sanity: opener_unilateral default close still works."""
    op_id = _open_op(client, space_id=space_id, title="default close")
    r = client.post(
        f"/v2/operations/{op_id}/close",
        json={"actor_handle": "@alice", "resolution": "answered"},
    )
    assert r.status_code == 200
