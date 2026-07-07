"""One-time interactive bootstrap of enterprise (Cowork) auth.

Everything else in :mod:`copilot.m365` runs headless off a refresh token (see
:mod:`copilot.m365.auth`) — but that first refresh token can only come from an
interactive sign-in. This opens a visible browser on the persistent profile,
lets you sign in with the work account, and **intercepts the MSAL ``/token``
response** to capture the refresh token (plus tenant + object id decoded from the
access token). It writes ``session/m365_token.json`` so
:meth:`copilot.m365.auth.M365Auth.load` works from then on.

We read the refresh token off the token *response* rather than out of MSAL's
localStorage because the M365 SPA does not leave a readable token cache on the
``m365.cloud.microsoft`` origin (confirmed by capture) — the response body is the
reliable source.

    python -m copilot.m365.bootstrap        # opens a browser; sign in with work account
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from .auth import AUTH_FILE

M365_CHAT_URL = "https://m365.cloud.microsoft/chat/"
DEFAULT_PROFILE_DIR = "session/profile"
_TOKEN_HINT = "/oauth2/v2.0/token"


def _decode_claims(token: str) -> dict:
    """Return a JWT's payload claims (unsigned decode), or ``{}`` on failure."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def bootstrap(path: str = AUTH_FILE, profile_dir: str = DEFAULT_PROFILE_DIR,
              timeout: int = 300, proxy: Optional[str] = None) -> dict:
    """Run the interactive capture and persist ``m365_token.json``; return it.

    Blocks until a ``/token`` response carrying a ``refresh_token`` is seen (the
    account is signed in and the SPA has fetched its first resource token) or
    ``timeout`` elapses. Raises ``RuntimeError`` if nothing was captured.
    """
    from playwright.sync_api import sync_playwright

    captured: dict = {}

    print("\n" + "=" * 70)
    print("Opening a browser. Sign in with your WORK account and wait for the")
    print("Microsoft 365 Copilot chat to load. This window captures the refresh")
    print("token and closes itself; nothing is sent anywhere.")
    print("=" * 70 + "\n")

    with sync_playwright() as pw:
        launch = dict(headless=False, args=["--disable-blink-features=AutomationControlled"],
                      user_agent=None)
        if proxy:
            from urllib.parse import urlparse
            u = urlparse(proxy)
            launch["proxy"] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        ctx = pw.chromium.launch_persistent_context(str(Path(profile_dir).resolve()),
                                                     **{k: v for k, v in launch.items() if v is not None})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp) -> None:
            if captured or _TOKEN_HINT not in resp.url:
                return
            try:
                data = resp.json()
            except Exception:
                return
            rt = data.get("refresh_token")
            at = data.get("access_token")
            if not rt:
                return
            claims = _decode_claims(at) if at else {}
            tenant = claims.get("tid") or _tenant_from_url(resp.url)
            oid = claims.get("oid")
            if tenant and oid:
                captured.update(refresh_token=rt, tenant=tenant, oid=oid)

        page.on("response", on_response)
        try:
            page.goto(M365_CHAT_URL, wait_until="domcontentloaded")
        except Exception:
            pass

        deadline = time.time() + timeout
        while not captured and time.time() < deadline:
            if page.is_closed():
                break
            page.wait_for_timeout(500)
        try:
            ctx.close()
        except Exception:
            pass

    if not captured:
        raise RuntimeError(
            "No refresh token captured. Make sure you signed in with the work "
            "account and the M365 Copilot chat finished loading before the timeout."
        )

    out = {"refresh_token": captured["refresh_token"], "tenant": captured["tenant"],
           "oid": captured["oid"], "tokens": {}}
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Saved enterprise auth to {path} (tenant={captured['tenant']}).")
    print("You can now mint tokens headless: copilot.m365.auth.M365Auth.load().token()")
    return out


def _tenant_from_url(url: str) -> Optional[str]:
    """Pull the tenant guid out of a ``…/<tenant>/oauth2/v2.0/token`` URL."""
    try:
        return url.split("login.microsoftonline.com/", 1)[1].split("/", 1)[0]
    except Exception:
        return None


if __name__ == "__main__":
    try:
        bootstrap()
    except Exception as exc:  # noqa: BLE001
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
