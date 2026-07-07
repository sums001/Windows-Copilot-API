"""Headless token minting for enterprise M365 Copilot (Copilot Cowork).

Captured from the real m365.cloud.microsoft web client (``tests/capture_m365.py``):
every resource token it uses — the Cowork agent runtime, substrate, sydney,
ic3.teams (Trouter), Graph … — is minted by a plain OAuth2 **public-client
refresh-token grant** against the tenant token endpoint. No browser, no client
secret, no proof-of-possession (the tokens are ordinary Bearer). So headless auth
reduces to:

  1. **Bootstrap once** from an interactive sign-in: capture the MSAL refresh
     token for the M365 Copilot SPA client (public client :data:`CLIENT_ID`) and
     the account's tenant + object id. Persisted to ``session/m365_token.json``.
  2. **Mint per resource** on demand: POST ``grant_type=refresh_token`` with
     ``scope=<resource>/.default``. Each response returns a short-lived access
     token *and a rotated refresh token*, which we persist for next time.

The request mirrors ``@azure/msal-browser`` exactly — ``redirect_uri=
brk-multihub://outlook.office.com`` plus the ``brk_*`` broker params — because
ESTS rejects the grant when they are missing. Scopes seen in the capture are in
:data:`SCOPES`; :data:`SCOPE_COWORK` is the one the chat *send* needs.

Conditional-access note: this works only if the tenant issues the account a
*non-bound* refresh token (as this one does). A tenant that requires a compliant/
managed device or app-bound tokens will reject the non-interactive grant — that
is a policy wall no client can code around.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from curl_cffi.requests import Session

from ..useragent import CHROME_UA

# Public SPA client id the M365 Copilot web app authenticates as (no secret).
CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
# Broker (Office) client the SPA rides; ESTS wants these echoed on every grant.
BROKER_CLIENT_ID = "4765445b-32c6-49b0-83e6-1d93765276ca"
REDIRECT_URI = "brk-multihub://outlook.office.com"
BROKER_REDIRECT_URI = "https://m365.cloud.microsoft/spalanding"
# ESTS registers c0ab8ce9 as a Single-Page-Application client, so it only redeems
# tokens on a *cross-origin* request — the grant must carry the SPA's Origin
# header (the browser adds it automatically; curl_cffi must send it explicitly or
# ESTS returns AADSTS9002327). This is the m365.cloud.microsoft page origin.
SPA_ORIGIN = "https://m365.cloud.microsoft"

# The Cowork agent's resource — the token the chat send (``/v1/messages``) carries.
COWORK_RESOURCE = "6ab48b67-cd74-4ad4-81af-5932984589be"

# Every scope observed being minted via refresh-token grant, keyed by short name.
# All are ``<resource>/.default`` public-client grants for the same refresh token.
SCOPES: Dict[str, str] = {
    "cowork": f"{COWORK_RESOURCE}/.default",          # chat send (aether runtime)
    "substrate": "https://substrate.office.com/.default",
    "substrate_search": "https://substrate.office.com/search/.default",
    "sydney": "https://substrate.office.com/sydney/.default",
    "ic3": "https://ic3.teams.office.com/.default",    # Trouter / Skype token
    "m365": "https://m365.cloud.microsoft/v2/.default",
    "graph": "https://graph.microsoft.com/.default",
}
SCOPE_COWORK = "cowork"

AUTH_FILE = "session/m365_token.json"
# Refresh a cached access token this many seconds before its stated expiry.
_EXPIRY_SKEW = 120


def token_endpoint(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def build_refresh_body(refresh_token: str, scope: str, oid: str, tenant: str) -> Dict[str, str]:
    """Return the form fields for a refresh-token grant, matching the capture.

    Pure — no I/O — so the exact wire shape (down to the broker params ESTS
    requires) is unit-tested without a network call. ``scope`` is a full
    ``<resource>/.default`` string; ``openid profile offline_access`` are appended
    as the web client does, so the response carries a rotated refresh token.
    """
    return {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": f"{scope} openid profile offline_access",
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "X-AnchorMailbox": f"Oid:{oid}@{tenant}",
        "brk_client_id": BROKER_CLIENT_ID,
        "brk_redirect_uri": BROKER_REDIRECT_URI,
        "x-client-SKU": "msal.js.browser",
        "x-client-VER": "5.9.0",
    }


class M365Auth:
    """Mints + caches per-resource access tokens from one bootstrapped refresh token.

    Persists ``{refresh_token, tenant, oid, tokens:{scope:{access_token,exp}}}`` to
    :data:`AUTH_FILE`. The refresh token rotates on every grant; the newest is
    saved so the bridge keeps working across restarts until the RT expires or the
    tenant revokes it. Construct from a saved file with :meth:`load`.
    """

    def __init__(self, refresh_token: str, tenant: str, oid: str,
                 path: str = AUTH_FILE, proxy: Optional[str] = None):
        self.refresh_token = refresh_token
        self.tenant = tenant
        self.oid = oid
        self.path = path
        self.proxy = proxy
        self._tokens: Dict[str, dict] = {}  # scope-name -> {access_token, exp}
        # Serialize minting: concurrent chats (e.g. a coding-agent firing several
        # calls at once) must not race the refresh-token rotation and lose the
        # newest RT. Cached tokens are read without the lock; only a mint locks.
        self._mint_lock = threading.Lock()

    @classmethod
    def load(cls, path: str = AUTH_FILE, proxy: Optional[str] = None) -> "M365Auth":
        """Rehydrate from :data:`AUTH_FILE`; raises if not bootstrapped yet."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not data.get("refresh_token"):
            raise RuntimeError(
                "No M365 refresh token saved. Bootstrap an enterprise sign-in first "
                "(interactive login that captures the refresh token)."
            )
        auth = cls(data["refresh_token"], data["tenant"], data["oid"], path=path, proxy=proxy)
        auth._tokens = data.get("tokens", {})
        return auth

    def token(self, scope_name: str = SCOPE_COWORK) -> str:
        """Return a valid access token for ``scope_name``, minting if needed.

        Reuses a cached token until it is within :data:`_EXPIRY_SKEW` of expiry,
        then does one refresh-token grant, caches the result, and persists the
        rotated refresh token.
        """
        if scope_name not in SCOPES:
            raise KeyError(f"unknown scope {scope_name!r}; known: {sorted(SCOPES)}")
        cached = self._tokens.get(scope_name)
        if cached and cached.get("exp", 0) - _EXPIRY_SKEW > time.time():
            return cached["access_token"]
        with self._mint_lock:
            # Re-check: another thread may have minted while we waited.
            cached = self._tokens.get(scope_name)
            if cached and cached.get("exp", 0) - _EXPIRY_SKEW > time.time():
                return cached["access_token"]
            return self._mint(scope_name)

    def _mint(self, scope_name: str) -> str:
        body = build_refresh_body(self.refresh_token, SCOPES[scope_name], self.oid, self.tenant)
        # Origin is mandatory for the SPA client (AADSTS9002327); Referer mirrors
        # what the browser sends so the request is a well-formed cross-origin call.
        headers = {
            "User-Agent": CHROME_UA,
            "Origin": SPA_ORIGIN,
            "Referer": f"{SPA_ORIGIN}/",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        with Session(proxy=self.proxy, headers=headers) as s:
            resp = s.post(token_endpoint(self.tenant), data=body)
        if resp.status_code != 200:
            raise RuntimeError(
                f"M365 token grant failed for {scope_name!r} ({resp.status_code}): "
                f"{resp.text[:300]}"
            )
        data = resp.json()
        access = data.get("access_token")
        if not access:
            raise RuntimeError(f"M365 token grant returned no access_token: {data}")
        # RTs rotate: keep the newest so the next mint (and next run) still works.
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]
        self._tokens[scope_name] = {
            "access_token": access,
            "exp": time.time() + int(data.get("expires_in", 3600)),
        }
        self._save()
        return access

    def _save(self) -> None:
        dest = Path(self.path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({
            "refresh_token": self.refresh_token,
            "tenant": self.tenant,
            "oid": self.oid,
            "tokens": self._tokens,
        }, indent=2), encoding="utf-8")
