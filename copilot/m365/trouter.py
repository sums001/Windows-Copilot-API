"""Socket.IO 0.9 frame codec for the Trouter push channel.

Trouter (``wss://<region>.trouter.teams.microsoft.com/v4/c``) speaks the classic
**Socket.IO 0.9** wire protocol — the framing captured off the real BizChat/Cowork
client (see ``tests/capture_m365.py``). A frame is::

    <type>:<id>:<endpoint>:<data>

where ``id`` and ``endpoint`` are usually empty and ``data`` (when present) is a
JSON string. Observed frames, all reproduced by :func:`decode`/:func:`encode`:

    1::                                     connect ack (server)
    5:::{"name":"user.authenticate",...}    event  (client -> server: send token)
    5:1+::{"name":"ping"}                    event  (client keepalive; id "1+")
    6:::1+["pong"]                           ack    (server -> client)
    3:::{"id":498…,"method":"POST",...}      message (server delivers a push)
    3:::{"id":498…,"status":200,...}         message (client ACKs that delivery)

The ``+`` suffix on an id marks that an ack is expected. Trouter tunnels each
inbound push as a **type-3 message** whose JSON body is itself an HTTP-request
shape (``{id, method, url, headers, body}``); the client must answer with a
type-3 message echoing the ``id`` and a ``status`` (see
:func:`copilot.m365.protocol.build_ack`). This module is pure framing only — no
I/O, no network — so it is unit-tested directly against captured frames.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

# Socket.IO 0.9 packet types.
TYPE_DISCONNECT = 0
TYPE_CONNECT = 1
TYPE_HEARTBEAT = 2
TYPE_MESSAGE = 3
TYPE_JSON = 4
TYPE_EVENT = 5
TYPE_ACK = 6
TYPE_ERROR = 7


@dataclass
class Packet:
    """A decoded Socket.IO 0.9 frame.

    ``id`` keeps its raw form including any trailing ``+`` (ack-requested), e.g.
    ``"1+"``; ``data`` is the raw trailing string (often JSON — use :meth:`json`).
    """

    type: int
    id: str
    endpoint: str
    data: str

    def json(self) -> Any:
        """Parse ``data`` as JSON, or return ``None`` if it isn't valid JSON."""
        if not self.data:
            return None
        try:
            return json.loads(self.data)
        except ValueError:
            return None

    @property
    def ack_requested(self) -> bool:
        """True when the peer wants an ack for this frame (id ends in ``+``)."""
        return self.id.endswith("+")


def decode(frame: str) -> Packet:
    """Decode one Socket.IO 0.9 frame string into a :class:`Packet`.

    Splits on ``:`` at most three times so a ``data`` segment containing colons
    (URLs, JSON) stays intact, and pads missing trailing segments so bare frames
    like ``"1::"`` decode without error.
    """
    parts = frame.split(":", 3)
    while len(parts) < 4:
        parts.append("")
    try:
        ptype = int(parts[0])
    except ValueError as exc:
        raise ValueError(f"not a Socket.IO frame: {frame!r}") from exc
    return Packet(ptype, parts[1], parts[2], parts[3])


def encode(packet: Packet) -> str:
    """Encode a :class:`Packet` back to the wire string.

    A frame with empty ``data`` is emitted as three segments (``"1::"``), matching
    the connect/heartbeat shape; otherwise all four segments are present.
    """
    head = f"{packet.type}:{packet.id}:{packet.endpoint}"
    return f"{head}:{packet.data}" if packet.data != "" else head


def event(name: str, args: Any, msg_id: str = "") -> str:
    """Build a type-5 event frame, e.g. ``user.authenticate`` or ``ping``.

    ``args`` is placed as the event's argument list (wrapped in a list if it
    isn't one already), matching ``{"name":..,"args":[..]}`` from the capture.
    """
    payload = {"name": name, "args": args if isinstance(args, list) else [args]}
    return encode(Packet(TYPE_EVENT, msg_id, "", json.dumps(payload, separators=(",", ":"))))


def message(obj: Any, msg_id: str = "") -> str:
    """Build a type-3 message frame carrying ``obj`` as JSON (used for ACKs)."""
    return encode(Packet(TYPE_MESSAGE, msg_id, "", json.dumps(obj, separators=(",", ":"))))


def authenticate_frame(access_token: str) -> str:
    """The first client frame: authenticate the socket with a Bearer token.

    Mirrors the captured ``5:::{"name":"user.authenticate","args":[{"headers":
    {"Authorization":"Bearer …","X-MS-Migration":"True"}}]}``. The token is the
    ``ic3.teams.office.com`` (Trouter/Skype) token, not the substrate one.
    """
    return event(
        "user.authenticate",
        [{"headers": {"Authorization": f"Bearer {access_token}", "X-MS-Migration": "True"}}],
    )


def ping_frame(seq: int) -> str:
    """A keepalive ``ping`` event; ``seq`` becomes the ack-requested id ``"<seq>+"``."""
    return event("ping", [], msg_id=f"{seq}+")


def is_pong(packet: Packet) -> bool:
    """True if ``packet`` is a server pong ack (``6:::N+["pong"]``)."""
    return packet.type == TYPE_ACK and "pong" in (packet.data or "")


def is_delivery(packet: Packet) -> bool:
    """True if ``packet`` is an inbound tunnelled push to ACK (a type-3 request).

    Distinguishes the server->client delivery (has ``method``) from our own
    client->server ACK (has ``status``)."""
    if packet.type != TYPE_MESSAGE:
        return False
    obj = packet.json()
    return isinstance(obj, dict) and "method" in obj and "status" not in obj
