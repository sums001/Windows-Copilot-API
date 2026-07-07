"""End-to-end enterprise Cowork test over the live Trouter channel (dev probe).

Mints the ic3 (Trouter) + Cowork tokens headless, opens the Trouter socket, and
once it's up fires the ``/v1/messages`` send — then waits for the reply to be
pushed back down the socket. This validates the full round trip and whether the
registrar step is needed (it runs WITHOUT registrar).

    python -m copilot.m365.probe "hello"
"""

from __future__ import annotations

import json
import sys
import uuid

from curl_cffi.requests import Session

from ..useragent import CHROME_UA
from .auth import M365Auth, SCOPE_COWORK
from .driver import DEFAULT_CONTAINER_CONFIG, DEFAULT_RUNTIME_HOST, new_conversation_id
from .trouter_client import TrouterReceiver

IC3_SCOPE = "ic3"


def main(argv) -> int:
    prompt = " ".join(argv) or "hello"
    auth = M365Auth.load()
    ic3 = auth.token(IC3_SCOPE)
    cowork = auth.token(SCOPE_COWORK)
    conv = new_conversation_id(auth.tenant, auth.oid)
    print(f"tokens minted (ic3 len={len(ic3)}, cowork len={len(cowork)})")
    print(f"conversation: {conv}\n")

    def send(_info) -> None:
        url = f"https://{DEFAULT_RUNTIME_HOST}/v1/messages"
        headers = {
            "User-Agent": CHROME_UA,
            "Authorization": f"Bearer {cowork}",
            "Content-Type": "application/json",
            "Origin": "https://m365.cloud.microsoft",
            "X-User-ID": auth.oid,
            "X-Container-Config": DEFAULT_CONTAINER_CONFIG,
        }
        body = {
            "connectorsConfig": {"connectors": [], "include_defaults": True, "packages": []},
            "content": [{"text": prompt, "type": "text"}],
            "conversationId": conv, "messageId": str(uuid.uuid4()), "role": "user",
        }
        with Session(timeout=60) as s:
            r = s.post(url, headers=headers, data=json.dumps(body))
        print(f"[send] {r.status_code} {r.text[:160]}")

    rx = TrouterReceiver(ic3, verbose=True)
    reply = rx.receive(send, timeout=120)

    print("\n" + "=" * 60)
    if reply:
        print("REPLY:\n" + reply)
    else:
        print("No reply received over Trouter within the timeout.")
        print("If deliveries=0, the backend needs the registrar step; if the socket "
              "never connected, check the ic3 token / WS URL.")
    print("=" * 60)
    return 0 if reply else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
