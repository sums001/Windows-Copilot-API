"""Capture the Microsoft 365 (enterprise) Copilot chat protocol — for building an
m365 driver.

Personal Copilot lives at copilot.microsoft.com; a work/school (Entra ID) account
is redirected to the *enterprise* Microsoft 365 Copilot at m365.cloud.microsoft,
whose chat backend the consumer driver (:mod:`copilot.driver`) does NOT speak.
This harness records that enterprise protocol from real traffic so the driver can
be written from evidence instead of guesses.

Run it signed in with your WORK account (the persistent profile under
``session/profile`` must already hold that sign-in — do ``python -m copilot
login`` with the work account first if not):

    python tests/capture_m365.py

It opens the signed-in profile, lands on the M365 Copilot chat (letting the
work-account redirect happen), and asks you to send ONE short message. Everything
below is appended, redacted, to ``session/m365_capture.log``:

  * WEBSOCKETS: every socket opened, its URL (query redacted), and every
    frame sent/received — this is the reply protocol if the transport is WS.
  * NETWORK: every request/response to office/substrate/m365 hosts — method,
    URL, status, content-type, whether an ``Authorization: Bearer`` header rode
    along, and (decoded from the JWT's *unsigned* payload) that token's ``aud``
    (resource) and ``scp`` (scopes). The request POST bodies are logged too:
    they are the chat-send payload shape the driver must reproduce.
  * TOKENS: the MSAL access-token entries in localStorage — their ``target``
    (scope) and ``clientId`` only, never the secret — so we learn which scope
    the enterprise chat call actually uses (vs consumer ``ChatAI.ReadWrite``).

SAFE TO SHARE: access tokens, JWTs, auth codes, and emails are redacted; only
non-secret token *claims* (aud/scp) are decoded and logged. Skim before posting
and attach ``session/m365_capture.log`` to the design discussion.
"""

import base64
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

# Run as a plain script: put the project root on sys.path so `copilot` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SESSION_DIR = Path("session")
PROFILE_DIR = SESSION_DIR / "profile"
CAPTURE_LOG = SESSION_DIR / "m365_capture.log"

# Where the work-account chat actually lives. We navigate to consumer Copilot and
# let it redirect (matches what a normal sign-in does), then also hard-nav to the
# M365 chat in case the profile lands somewhere neutral.
START_URL = "https://copilot.microsoft.com/"
M365_CHAT_URL = "https://m365.cloud.microsoft/chat/"

# Hosts whose traffic is the enterprise chat protocol (everything else is noise).
# Enterprise BizChat sends the query to Substrate/Sydney (substrate.office.com)
# and receives the streamed reply over a Trouter push channel on
# *.trouter.teams.microsoft.com — so those hosts must be in scope or the actual
# chat protocol is invisible (the first capture missed it).
_INTERESTING_HOST_HINTS = (
    "cloud.microsoft", "office.com", "substrate", "m365", "copilot",
    "trouter", "teams.microsoft", "sydney", "bing.com", "skype",
)

_CAPTURE_TICKS = 600  # 600 * 500ms = 5 min ceiling


# --- redaction (standalone copy of diagnostic.py's, so this script is portable) --
_REDACTORS = [
    (re.compile(r"(accessToken|access_token)=[^&\s\"]+", re.I), r"\1=<REDACTED>"),
    (re.compile(r"(code|client_info|state|nonce|epct|epctrc|reconnectionToken)=[^&\s\"]+", re.I), r"\1=<REDACTED>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)?"), "<JWT>"),
    (re.compile(r"\bM\.[A-Za-z0-9_\-.!*$%]{20,}"), "<AUTHCODE>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    (re.compile(r"[A-Za-z0-9_\-]{40,}"), "<REDACTED>"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTORS:
        text = pattern.sub(repl, text)
    return text


def _to_text(payload) -> str:
    if isinstance(payload, (bytes, bytearray)):
        return payload.decode("utf-8", "replace")
    return str(payload)


def _interesting(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(hint in host for hint in _INTERESTING_HOST_HINTS)


def _jwt_claims(bearer: str) -> str:
    """Return non-secret ``aud``/``scp``/``appid`` from a Bearer JWT's payload.

    Decodes only the middle (payload) segment — never verifies or logs the
    signature — to reveal which resource+scope the enterprise chat call uses.
    Returns "" if the header isn't a decodable JWT."""
    try:
        tok = bearer.split(None, 1)[1] if bearer.lower().startswith("bearer ") else bearer
        parts = tok.split(".")
        if len(parts) < 2:
            return ""
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(pad))
        keep = {k: claims.get(k) for k in ("aud", "scp", "appid", "roles", "iss") if k in claims}
        return json.dumps(keep, ensure_ascii=False)
    except Exception:
        return ""


# List MSAL AccessToken entries' scope/client only (never the secret).
_LIST_TOKENS_JS = """
() => {
  const out = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k);
      if (v && v.indexOf('"credentialType":"AccessToken"') !== -1) {
        try {
          const o = JSON.parse(v);
          out.push({target: o.target || null, clientId: o.clientId || null,
                    realm: o.realm || null, secretLen: (o.secret||'').length});
        } catch (e) {}
      }
    }
  } catch (e) {}
  return out;
}
"""


def main() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    sink = CAPTURE_LOG.open("w", encoding="utf-8")

    def log(tag: str, payload="") -> None:
        if not sink.closed:
            sink.write(redact(f"{tag} {_to_text(payload)}").rstrip() + "\n")
            sink.flush()

    print("\n" + "=" * 70)
    print("Opening your signed-in (work-account) profile.")
    print("IMPORTANT: wait for the M365 Copilot chat to fully load, then TYPE A")
    print("MESSAGE (e.g. 'hello') AND SEND IT. Wait for the full reply to finish")
    print("streaming — the reply arrives over a Trouter channel and is the main")
    print("thing we need to capture. Only THEN close the window.")
    print(f"Frames + requests stream to  {CAPTURE_LOG}")
    print("Auto-stops after 5 min.")
    print("=" * 70 + "\n")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        print(f"Playwright unavailable: {e}")
        return

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR.resolve()),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # --- WebSocket transport ------------------------------------------
        # Log EVERY socket, not just "interesting" hosts: Trouter/Sydney reply
        # channels live on hosts (trouter.teams.microsoft.com) the request filter
        # would otherwise drop, and the reply frames are the whole point.
        def on_ws(ws) -> None:
            log("[WS-OPEN]", ws.url)
            ws.on("framesent", lambda *a: log("[WS-SENT]", a[0] if a else b""))
            ws.on("framereceived", lambda *a: log("[WS-RECV]", a[0] if a else b""))
            ws.on("close", lambda *a: log("[WS-CLOSE]", ws.url))

        # --- HTTP transport (SSE / fetch streaming / plain POST) ----------
        def on_request(req) -> None:
            # Log every non-GET (POST/PUT/PATCH) regardless of host — the outbound
            # message send goes to a Teams messaging host that the interesting-host
            # filter misses, and it's the one request we still need. GETs stay
            # filtered to avoid drowning in static-asset noise.
            if req.method == "GET" and not _interesting(req.url):
                return
            hdrs = {}
            try:
                hdrs = req.headers
            except Exception:
                pass
            auth = hdrs.get("authorization", "")
            claims = _jwt_claims(auth) if auth else ""
            log("[REQ]", f"{req.method} {req.url}")
            if auth:
                log("      auth", f"present; claims={claims}")
            for h in ("content-type", "x-anchormailbox", "x-routingparameter-sessionkey",
                      "x-scenario", "x-clientcorrelationid", "correlationvector"):
                if h in hdrs:
                    log("      hdr", f"{h}: {hdrs[h]}")
            body = None
            try:
                body = req.post_data
            except Exception:
                pass
            if body:
                log("      body", body[:4000])

        def on_response(resp) -> None:
            if not _interesting(resp.url):
                return
            ct = ""
            try:
                ct = resp.headers.get("content-type", "")
            except Exception:
                pass
            log("[RESP]", f"{resp.status} {resp.url}  ct={ct}")

        page.on("websocket", on_ws)
        page.on("request", on_request)
        page.on("response", on_response)

        try:
            page.goto(START_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            # If the redirect didn't land us on the chat, nudge it there.
            if "m365.cloud.microsoft" not in (page.url or ""):
                try:
                    page.goto(M365_CHAT_URL, wait_until="domcontentloaded")
                except Exception:
                    pass
        except Exception:
            pass

        # Snapshot the token scopes present (scope-only, no secrets).
        try:
            toks = page.evaluate(_LIST_TOKENS_JS)
            log("[TOKENS]", json.dumps(toks, ensure_ascii=False))
        except Exception:
            pass

        ticks = _CAPTURE_TICKS
        try:
            while not page.is_closed() and ticks > 0:
                page.wait_for_timeout(500)
                ticks -= 1
        except Exception:
            pass

        # Re-snapshot tokens at the end: the chat token may only mint on the turn.
        try:
            if not page.is_closed():
                toks = page.evaluate(_LIST_TOKENS_JS)
                log("[TOKENS-END]", json.dumps(toks, ensure_ascii=False))
        except Exception:
            pass

        sink.close()
        try:
            ctx.close()
        except Exception:
            pass

    print("\n" + "=" * 62)
    print(f"Capture written to {CAPTURE_LOG}")
    print("Secrets are redacted. Skim it, then share it so the m365 driver can be")
    print("designed from the real protocol (transport, endpoints, token scope).")


if __name__ == "__main__":
    main()
