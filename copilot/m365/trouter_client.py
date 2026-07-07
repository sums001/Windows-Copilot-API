"""Live Trouter receiver — the Cowork reply channel.

The enterprise reply is delivered only over Trouter (Teams' Socket.IO push
channel); the Aether runtime exposes no REST read (confirmed: ``/v1/messages`` is
POST-only, ``/v1/conversations/<id>`` is DELETE-only). Captured handshake
(``tests/capture_m365.py``):

    GET  go-eu.trouter.teams.microsoft.com/?check=…&cor_id=…&epid=…&tc=…
    WSS  go-eu.trouter.teams.microsoft.com/v4/c?tc=…&timeout=40&epid=…&cor_id=…&con_num=…
    <-  1::                                    (connect)
    ->  5:::{"name":"user.authenticate", …Bearer <ic3 token>…}
    <-  5:1::{"name":"trouter.connected", {id, surl, registrarUrl, …}}
    <-  3:::{… "body":"{…\"messagetype\":\"RichText/Copilot_AgentResponse\" …}"}   (reply)
    ->  3:::{"id":…,"status":200, …}            (ACK)

The reply arrives as a single delivery carrying the full ``content`` +
``copilotTaskState:"completed"`` (see :mod:`copilot.m365.protocol`). This client
connects, authenticates with the ``ic3.teams.office.com`` token, fires the send
via a callback once the channel is up, and returns the first completed reply.

Registrar note: whether the backend needs an explicit registrar POST to route the
reply here is untested — :class:`TrouterReceiver` runs without it so we can learn
empirically; if no delivery arrives, the registrar step is added.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Callable, List, Optional
from urllib.parse import quote

from curl_cffi.const import CurlECode, CurlInfo
from curl_cffi.curl import CurlError
from curl_cffi.requests import Session, CurlWsFlag
from select import select

from ..useragent import CHROME_UA
from . import protocol, trouter

TROUTER_HOST = "go-eu.trouter.teams.microsoft.com"
# The client-descriptor query the web app sends on every Trouter call.
_TC = {"cv": "2025.30.01.1", "ua": "BizChat", "hr": "", "v": "3639/1.0.0"}
_CURL_SOCKET_BAD = -1

# Where the endpoint is registered so the messaging backend routes the Cowork
# reply to our Trouter path. Body shape captured from the real client
# (tests/capture_registrar.py): registrationId == our WS epid, the TROUTER path
# is the surl from the trouter.connected frame.
REGISTRAR_URL = "https://edge.skype.com/registrar/prod/V3/registrations"
_CLIENT_DESCRIPTION = {
    "appId": "bizchat",
    "aesKey": "",
    "languageId": "en-US",
    "platform": "3639/1.0.0",
    "templateKey": "bizchat_5.0",
    "platformUIVersion": "3639/1.0.0",
    "productContext": "COPILOT",
}


def _tc_param() -> str:
    return quote(json.dumps(_TC, separators=(",", ":")))


class TrouterReceiver:
    """Connects the Trouter socket and returns one completed Cowork reply."""

    def __init__(self, ic3_token: str, proxy: Optional[str] = None, verbose: bool = False):
        self.ic3_token = ic3_token
        self.proxy = proxy
        self.verbose = verbose
        self.connected_info: Optional[dict] = None
        self._deliveries = 0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[trouter] {msg}", flush=True)

    def receive(self, on_connected: Callable[[dict], None], timeout: int = 120) -> str:
        """Open the socket, authenticate, call ``on_connected`` once the channel
        is up (fire the chat send there), and return the first completed reply's
        text. Returns ``""`` if none arrives before ``timeout``.
        """
        epid = str(uuid.uuid4())
        cor = str(uuid.uuid4())
        con_num = f"{int(time.time() * 1000)}_0"
        ws_url = (f"wss://{TROUTER_HOST}/v4/c?tc={_tc_param()}&timeout=40&epid={epid}"
                  f"&ccid=&dom=m365.cloud.microsoft&cor_id={cor}&con_num={con_num}")

        segments: List[str] = []
        full_reply = ""
        with Session(proxy=self.proxy, timeout=timeout + 30) as s:
            # Pre-GET the mapping endpoint the web client hits first (best-effort).
            try:
                s.get(f"https://{TROUTER_HOST}/?check={int(time.time() * 1000)}"
                      f"&cor_id={cor}&epid={epid}&tc={_tc_param()}",
                      headers={"User-Agent": CHROME_UA})
            except Exception:
                pass

            ws = s.ws_connect(ws_url, headers={"User-Agent": CHROME_UA})
            self._log(f"socket open (epid={epid})")
            deadline = time.time() + timeout
            fired = False
            ping_seq = 0
            last_ping = time.time()

            while time.time() < deadline:
                frame = self._recv_text(ws, min(deadline, time.time() + 20))
                if frame is None:
                    # Idle: keepalive ping so Trouter doesn't drop us.
                    if time.time() - last_ping > 15:
                        ping_seq += 1
                        ws.send(trouter.ping_frame(ping_seq).encode(), CurlWsFlag.TEXT)
                        last_ping = time.time()
                    continue

                pkt = trouter.decode(frame)
                if pkt.type == trouter.TYPE_CONNECT:            # '1::'
                    ws.send(trouter.authenticate_frame(self.ic3_token).encode(), CurlWsFlag.TEXT)
                    self._log("authenticated")
                    continue

                obj = pkt.json()
                if pkt.type == trouter.TYPE_EVENT and isinstance(obj, dict):
                    if obj.get("name") == "trouter.connected":
                        self.connected_info = (obj.get("args") or [{}])[0]
                        self._log("trouter.connected")
                        if not fired:
                            fired = True
                            # Register the endpoint FIRST so the messaging backend
                            # routes the reply to us, THEN fire the send.
                            self._register(epid, self.connected_info)
                            on_connected(self.connected_info)
                        continue
                    # ignore trouter.message_loss and other control events

                if trouter.is_delivery(pkt):
                    delivery = pkt.json()
                    self._deliveries += 1
                    # ACK immediately or Trouter redelivers.
                    ws.send(protocol.build_ack(delivery).encode(), CurlWsFlag.TEXT)
                    ev = protocol.parse_delivery(delivery)
                    if ev and ev.text:
                        self._log(f"reply frame (completed={ev.completed})")
                        # The agent response carries the full text in one frame;
                        # keep the longest/last as the answer.
                        full_reply = ev.text if len(ev.text) >= len(full_reply) else full_reply
                        if ev.completed:
                            return full_reply
            self._log(f"timeout after {self._deliveries} deliveries")
        return full_reply

    def _register(self, epid: str, info: dict) -> None:
        """Register our Trouter endpoint so the reply is routed to this socket.

        ``registrationId`` is our WS ``epid``; the TROUTER ``path`` is the ``surl``
        the server handed back in ``trouter.connected``. Bearer is the
        ``ic3.teams.office.com`` token. Body shape captured from the real client.
        """
        surl = info.get("surl")
        if not surl:
            self._log("no surl in trouter.connected; cannot register")
            return
        body = {
            "clientDescription": _CLIENT_DESCRIPTION,
            "registrationId": epid,
            "nodeId": "",
            "transports": {"TROUTER": [{"context": "", "path": surl, "ttl": 3600}]},
        }
        try:
            with Session(proxy=self.proxy, timeout=30) as s:
                r = s.post(REGISTRAR_URL, headers={
                    "User-Agent": CHROME_UA,
                    "Authorization": f"Bearer {self.ic3_token}",
                    "Content-Type": "application/json",
                }, data=json.dumps(body))
            self._log(f"registrar -> {r.status_code}")
        except Exception as e:  # noqa: BLE001
            self._log(f"registrar error: {e}")

    @staticmethod
    def _recv_text(ws, deadline: float) -> Optional[str]:
        """Read one full WS text frame, or None past ``deadline`` (idle)."""
        sock_fd = ws.curl.getinfo(CurlInfo.ACTIVESOCKET)
        if sock_fd == _CURL_SOCKET_BAD:
            raise ConnectionError("Trouter socket has no active socket")
        chunks = []
        while True:
            try:
                chunk, frame = ws.recv_fragment()
                chunks.append(chunk)
                if frame.bytesleft == 0 and frame.flags & CurlWsFlag.CONT == 0:
                    data = b"".join(chunks)
                    return data.decode("utf-8", "replace")
            except CurlError as e:
                if e.code != CurlECode.AGAIN:
                    raise
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                select([sock_fd], [], [], min(0.5, remaining))
