"""Unit tests for the enterprise (Copilot Cowork) Trouter codec + reply parser.

Frames here are the *real* ones captured off m365.cloud.microsoft with
``tests/capture_m365.py`` (thread ids reduced to placeholders). They pin the
Socket.IO 0.9 framing and the ``Copilot_AgentResponse`` shape so the driver can
be built and refactored against a fixed contract without a live account.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.m365 import protocol, trouter


# --- Socket.IO framing (captured client/server frames) ----------------------

def test_decode_connect_ack():
    p = trouter.decode("1::")
    assert (p.type, p.id, p.endpoint, p.data) == (trouter.TYPE_CONNECT, "", "", "")


def test_decode_event_with_json_data_keeps_colons():
    # URL/JSON in data contains colons; only the first three ':' are separators.
    frame = '3:::{"id":42,"url":"https://x/y","status":200}'
    p = trouter.decode(frame)
    assert p.type == trouter.TYPE_MESSAGE
    assert p.json()["url"] == "https://x/y"


def test_encode_roundtrip_event():
    frame = trouter.event("ping", [], msg_id="1+")
    p = trouter.decode(frame)
    assert p.type == trouter.TYPE_EVENT and p.id == "1+" and p.ack_requested
    assert p.json() == {"name": "ping", "args": []}


def test_authenticate_frame_shape():
    p = trouter.decode(trouter.authenticate_frame("TOK"))
    args = p.json()["args"][0]
    assert args["headers"]["Authorization"] == "Bearer TOK"
    assert args["headers"]["X-MS-Migration"] == "True"


def test_is_pong_and_is_delivery():
    assert trouter.is_pong(trouter.decode('6:::1+["pong"]'))
    assert not trouter.is_pong(trouter.decode("1::"))
    # A server delivery has method (no status); our own ACK has status (no method).
    delivery = '3:::{"id":1,"method":"POST","url":"/m","headers":{},"body":"{}"}'
    ack = '3:::{"id":1,"status":200,"headers":{},"body":""}'
    assert trouter.is_delivery(trouter.decode(delivery))
    assert not trouter.is_delivery(trouter.decode(ack))


# --- Cowork reply parsing (the captured agent response) ---------------------

def _agent_delivery(content, task_state="completed", segments=None):
    """Wrap a resource in the delivery shape Trouter tunnels (body is a string)."""
    props = {
        "copilotTaskState": task_state,
        "copilotConversationId": "00000000-0000-0000-0000-000000000conv",
    }
    if segments is not None:
        props["copilotTextSegments"] = [{"id": f"t{i}", "text": t} for i, t in enumerate(segments)]
    body = {
        "type": "EventMessage",
        "resourceType": "NewMessage",
        "resource": {
            "id": "1783418253839",
            "content": content,
            "messagetype": protocol.AGENT_RESPONSE_TYPE,
            "properties": props,
        },
    }
    return {"id": 1183867798, "method": "POST", "url": "/v4/f/x/messaging",
            "headers": {"MS-CV": "abc", "trouter-request": "{}"}, "body": json.dumps(body)}


def test_parse_real_agent_response():
    content = ("Hi there! I'm here to help you with your email, calendar, Teams, "
               "files, and documents — or anything else you're working on in "
               "Microsoft 365.\n\nWhat can I do for you today?")
    ev = protocol.parse_delivery(_agent_delivery(
        content, segments=["Hi", " there! …", " What can I do for you today?"]))
    assert ev is not None
    assert ev.text == content            # full content preferred over segments
    assert ev.completed is True
    assert ev.conversation_id == "00000000-0000-0000-0000-000000000conv"
    assert len(ev.segments) == 3


def test_text_falls_back_to_segments_when_no_content():
    ev = protocol.parse_delivery(_agent_delivery("", segments=["Hel", "lo"]))
    assert ev.text == "Hello"


def test_incomplete_turn_not_completed():
    ev = protocol.parse_delivery(_agent_delivery("thinking…", task_state="running"))
    assert ev is not None and ev.completed is False


def test_user_echo_is_ignored():
    body = {"type": "EventMessage", "resourceType": "NewMessage",
            "resource": {"content": "Hello", "messagetype": "Text",
                         "from": "…/8:orgid:00000000", "properties": {}}}
    delivery = {"id": 451502264, "method": "POST", "url": "/m", "headers": {}, "body": json.dumps(body)}
    assert protocol.parse_delivery(delivery) is None


def test_thread_update_is_ignored():
    body = {"type": "EventMessage", "resourceType": "ThreadUpdate",
            "resource": {"id": "19:x@thread", "type": "Thread", "properties": {"productThreadType": "cowork"}}}
    delivery = {"id": 498345833, "method": "POST", "url": "/m", "headers": {}, "body": json.dumps(body)}
    assert protocol.parse_delivery(delivery) is None


def test_build_ack_echoes_id_and_status():
    delivery = {"id": 995027863, "method": "POST", "url": "/m",
                "headers": {"MS-CV": "cv123", "trouter-request": "{\"id\":\"r\"}"}, "body": "{}"}
    ack = protocol.build_ack(delivery)
    p = trouter.decode(ack)
    obj = p.json()
    assert obj["id"] == 995027863 and obj["status"] == 200
    assert obj["headers"]["MS-CV"] == "cv123"
    assert obj["body"] == ""
