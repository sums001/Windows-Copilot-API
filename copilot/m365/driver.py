"""Pure-HTTP driver for enterprise M365 Copilot (Copilot Cowork).

Validated end-to-end against a real work tenant. Cowork is a Copilot Studio
("Aether") agent whose transport is split:

  * **send**  — ``POST https://<runtime>/v1/messages`` (Cowork-resource Bearer);
    returns ``{"status":"accepted"}`` — fire-and-forget.
  * **receive** — the reply is pushed over **Trouter** (Teams' Socket.IO channel);
    the Aether runtime exposes no REST read. So we open the Trouter socket, mint
    the ``ic3.teams.office.com`` token, register the endpoint, and read the reply
    off the socket. See :mod:`copilot.m365.trouter_client`.

All tokens are minted headless from one bootstrapped refresh token
(:mod:`copilot.m365.auth`) — no browser, no Cloudflare. The whole flow:

    tokens (refresh-grant) -> Trouter connect+auth -> registrar -> send -> reply

    from copilot.m365 import M365Copilot
    print(M365Copilot().ask("hello"))
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Optional

from curl_cffi.requests import Session

from ..useragent import CHROME_UA
from .auth import M365Auth, SCOPE_COWORK
from .trouter_client import TrouterReceiver

# Discovered per-geo/tenant via a routing call and cached by the web client
# ("RoutingCacheHit"); stable per tenant. Override for other tenants/geos.
DEFAULT_RUNTIME_HOST = "mcsaetherruntime-neu.eu-ia102.gateway.prod.island.powerapps.com"
IC3_SCOPE = "ic3"

# Feature/model flags the web client sends; ``model`` selects the backend.
DEFAULT_CONTAINER_CONFIG = (
    "renderUi=true;searchBackend=bing;citationsEnabled=true;acceptLanguage=en-US;model=fable-5:claude"
)


def new_conversation_id(tenant: str, oid: str) -> str:
    """A fresh Cowork conversation id: ``<tenant>:<oid>:<taskGuid>``."""
    return f"{tenant}:{oid}:{uuid.uuid4()}"


class M365Copilot:
    """Chat with enterprise Copilot Cowork, headless (send + Trouter receive)."""

    def __init__(self, auth: Optional[M365Auth] = None, runtime_host: str = DEFAULT_RUNTIME_HOST,
                 container_config: str = DEFAULT_CONTAINER_CONFIG,
                 proxy: Optional[str] = None, timeout: Optional[int] = None):
        self.auth = auth or M365Auth.load(proxy=proxy)
        self.runtime_host = runtime_host
        self.container_config = container_config
        self.proxy = proxy
        # Real Cowork turns have been observed at up to ~110s; 120s left almost
        # no headroom, so slow-but-successful turns died as timeouts. Tunable
        # because acceptable wait is a deployment choice (M365_TIMEOUT, seconds).
        self.timeout = timeout if timeout is not None else int(os.environ.get("M365_TIMEOUT", "180"))
        self.last_conversation_id: Optional[str] = None

    def ask(self, prompt: str, conversation_id: Optional[str] = None) -> str:
        """Send ``prompt`` and return the full reply text.

        Starts a new conversation when ``conversation_id`` is None; pass a prior
        :attr:`last_conversation_id` back to continue the same thread. Blocks up
        to ``timeout`` per attempt for the reply (model think-time is ~10-30s,
        long agent turns up to ~2min).

        If the socket saw *zero* deliveries by the deadline, the Trouter
        registration or route likely went stale (observed as every subsequent
        turn hanging forever). One retry runs on a brand-new socket, endpoint id
        and registration — re-sending the prompt on the same conversation. A
        duplicate send is harmless here: we take the first completed reply.
        """
        conv = conversation_id or new_conversation_id(self.auth.tenant, self.auth.oid)
        self.last_conversation_id = conv
        cowork = self.auth.token(SCOPE_COWORK)
        ic3 = self.auth.token(IC3_SCOPE)

        def send(_connected_info) -> None:
            self._send(conv, prompt, cowork)

        deliveries = 0
        for attempt in (1, 2):
            receiver = TrouterReceiver(ic3, proxy=self.proxy)
            reply = receiver.receive(send, timeout=self.timeout)
            deliveries = receiver._deliveries
            if reply:
                return reply
            if deliveries:
                # The route works (frames arrived) but the turn never completed;
                # a fresh socket + duplicate send won't finish it any faster.
                break
        raise RuntimeError(
            "No reply received from Copilot Cowork within "
            f"{self.timeout}s (deliveries={deliveries}, attempts={attempt}). "
            "The turn may still be running, or the Trouter registration/route changed."
        )

    def _send(self, conversation_id: str, prompt: str, cowork_token: str) -> None:
        url = f"https://{self.runtime_host}/v1/messages"
        headers = {
            "User-Agent": CHROME_UA,
            "Authorization": f"Bearer {cowork_token}",
            "Content-Type": "application/json",
            "Origin": "https://m365.cloud.microsoft",
            "X-User-ID": self.auth.oid,
            "X-Container-Config": self.container_config,
        }
        body = {
            "connectorsConfig": {"connectors": [], "include_defaults": True, "packages": []},
            "content": [{"text": prompt, "type": "text"}],
            "conversationId": conversation_id,
            "messageId": str(uuid.uuid4()),
            "role": "user",
        }
        with Session(proxy=self.proxy, timeout=60) as s:
            r = s.post(url, headers=headers, data=json.dumps(body))
        if r.status_code >= 400:
            raise RuntimeError(f"Cowork send failed ({r.status_code}): {r.text[:300]}")


def main(argv) -> int:
    prompt = " ".join(argv) or "Hello"
    print(M365Copilot().ask(prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
