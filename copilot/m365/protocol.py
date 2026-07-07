"""Parse Copilot Cowork replies out of Trouter deliveries; build the ACK.

Every inbound push arrives as a Socket.IO type-3 message whose JSON is an
HTTP-request shape (:func:`copilot.m365.trouter.is_delivery`)::

    {"id": 498…, "method": "POST", "url": ".../messaging",
     "headers": {…}, "body": "<a JSON *string*>"}

The ``body`` string decodes to a Teams messaging event::

    {"type":"EventMessage","resourceType":"NewMessage",
     "resource":{"content":"…","messagetype":"RichText/Copilot_AgentResponse",
                 "properties":{"copilotTextSegments":[{"id":"t0","text":"Hi"},…],
                               "copilotTaskState":"completed",
                               "copilotConversationId":"…"}}}

We care about three ``resource`` shapes seen in the capture:

  * ``messagetype == "RichText/Copilot_AgentResponse"`` — the assistant's reply.
    Text is in ``content`` (full) and streamed in ``copilotTextSegments``;
    ``copilotTaskState == "completed"`` marks the turn done.
  * ``messagetype == "Text"`` from an ``8:orgid:`` sender — the echo of the
    user's own message; ignored.
  * ``resourceType == "ThreadUpdate"`` — thread state (``copilotMetadata.state``
    e.g. ``response_sent``); carries no reply text.

Each delivery must be answered with a type-3 ACK echoing the ``id`` and
``status:200`` (see :func:`build_ack`), or Trouter re-delivers.

Pure parsing only — unit-tested against captured frames.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .trouter import Packet, TYPE_MESSAGE, decode, encode

AGENT_RESPONSE_TYPE = "RichText/Copilot_AgentResponse"


@dataclass
class ReplyEvent:
    """A parsed assistant reply from one Trouter delivery.

    ``text`` is the best full-text for this frame (``content`` when present, else
    the concatenated segments). ``completed`` is True once the backend marks
    ``copilotTaskState == "completed"`` — the caller stops reading the turn then.
    """

    text: str
    completed: bool
    conversation_id: Optional[str] = None
    segments: List[str] = field(default_factory=list)
    reasoning: List[str] = field(default_factory=list)
    message_id: Optional[str] = None


def _delivery_body(delivery: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Decode a delivery's ``body`` (a JSON *string*) into the messaging event."""
    body = delivery.get("body")
    if not isinstance(body, str) or not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def parse_delivery(delivery: Dict[str, Any]) -> Optional[ReplyEvent]:
    """Return a :class:`ReplyEvent` if ``delivery`` carries an assistant reply.

    ``delivery`` is the decoded type-3 JSON (``{id, method, url, headers, body}``).
    Returns ``None`` for user echoes, thread updates, notifications, and anything
    that isn't a ``Copilot_AgentResponse`` — the caller ACKs those but yields no
    text.
    """
    event = _delivery_body(delivery)
    if not event:
        return None
    resource = event.get("resource")
    if not isinstance(resource, dict):
        return None
    if resource.get("messagetype") != AGENT_RESPONSE_TYPE:
        return None

    props = resource.get("properties") or {}
    segments = [s.get("text", "") for s in props.get("copilotTextSegments", []) if isinstance(s, dict)]
    reasoning = [r.get("content", "") for r in props.get("copilotReasoning", []) if isinstance(r, dict)]
    content = resource.get("content")
    text = content if isinstance(content, str) and content else "".join(segments)

    return ReplyEvent(
        text=text,
        completed=props.get("copilotTaskState") == "completed",
        conversation_id=props.get("copilotConversationId"),
        segments=segments,
        reasoning=reasoning,
        message_id=resource.get("id"),
    )


def build_ack(delivery: Dict[str, Any], status: int = 200) -> str:
    """Build the type-3 ACK frame for an inbound delivery.

    Echoes the delivery ``id`` and the ``trouter-request``/``MS-CV`` headers back
    with a status, matching the captured client ACK
    ``3:::{"id":…,"status":200,"headers":{…},"body":""}``. Without this ACK the
    Trouter backend redelivers the message.
    """
    in_headers = delivery.get("headers") or {}
    ack_headers: Dict[str, Any] = {}
    for h in ("MS-CV", "trouter-request"):
        if h in in_headers:
            ack_headers[h] = in_headers[h]
    ack_headers["trouter-client"] = {"cd": 1}
    ack = {"id": delivery.get("id"), "status": status, "headers": ack_headers, "body": ""}
    return encode(Packet(TYPE_MESSAGE, "", "", json.dumps(ack, separators=(",", ":"))))
