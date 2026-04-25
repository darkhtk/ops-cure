from __future__ import annotations

from pc_launcher.bridge_client import parse_sse_stream


def _stream(*lines: str):
    return iter(lines)


def test_parse_sse_stream_decodes_typed_event_with_json_payload():
    events = list(
        parse_sse_stream(
            _stream(
                "event: event",
                'data: {"cursor": "001", "space_id": "machine-a", "event": {"id": "cmd-1", "kind": "remote_codex.command.queued", "actor_name": "homedev", "content": "{}"}}',
                "",
            )
        )
    )
    assert len(events) == 1
    assert events[0]["event"] == "event"
    payload = events[0]["data"]
    assert isinstance(payload, dict)
    assert payload["space_id"] == "machine-a"
    assert payload["event"]["kind"] == "remote_codex.command.queued"


def test_parse_sse_stream_handles_multiple_events_and_heartbeats():
    events = list(
        parse_sse_stream(
            _stream(
                "event: open",
                'data: {"space_id": "machine-a"}',
                "",
                "event: heartbeat",
                'data: {"space_id": "machine-a", "cursor": null}',
                "",
                "event: event",
                'data: {"cursor": "002"}',
                "",
            )
        )
    )
    assert [item["event"] for item in events] == ["open", "heartbeat", "event"]
    assert events[0]["data"] == {"space_id": "machine-a"}


def test_parse_sse_stream_ignores_comments_and_keepalive():
    events = list(
        parse_sse_stream(
            _stream(
                ": keep-alive comment",
                "event: heartbeat",
                'data: {"cursor": "001"}',
                "",
            )
        )
    )
    assert events == [{"event": "heartbeat", "data": {"cursor": "001"}}]


def test_parse_sse_stream_passes_non_json_payload_as_string():
    events = list(parse_sse_stream(_stream("event: hello", "data: world", "")))
    assert events == [{"event": "hello", "data": "world"}]


def test_parse_sse_stream_flushes_trailing_event_without_blank_line():
    events = list(
        parse_sse_stream(
            _stream(
                "event: event",
                'data: {"x": 1}',
            )
        )
    )
    assert events == [{"event": "event", "data": {"x": 1}}]


def test_parse_sse_stream_handles_unnamed_event_as_message():
    events = list(parse_sse_stream(_stream("data: hi", "")))
    assert events == [{"event": "message", "data": "hi"}]
