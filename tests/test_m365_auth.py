"""Unit tests for the enterprise refresh-token grant construction.

The request body is asserted against the exact shape captured from the real
m365.cloud.microsoft client (``tests/capture_m365.py``), so a future refactor
can't silently drop a field ESTS requires (the ``brk_*`` broker params, the
``.default`` scope, the anchor mailbox).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.m365 import auth


def test_refresh_body_matches_captured_shape():
    body = auth.build_refresh_body(
        refresh_token="1.RT.VALUE",
        scope=auth.SCOPES["cowork"],
        oid="00000000-0000-0000-0000-0000000000oid",
        tenant="00000000-0000-0000-0000-00000000tenant",
    )
    assert body["client_id"] == auth.CLIENT_ID
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "1.RT.VALUE"
    assert body["redirect_uri"] == "brk-multihub://outlook.office.com"
    assert body["brk_client_id"] == auth.BROKER_CLIENT_ID
    assert body["brk_redirect_uri"] == "https://m365.cloud.microsoft/spalanding"
    # scope keeps the captured resource + the offline_access that rotates the RT.
    assert body["scope"].startswith("6ab48b67-cd74-4ad4-81af-5932984589be/.default")
    assert "offline_access" in body["scope"]
    assert body["X-AnchorMailbox"] == "Oid:00000000-0000-0000-0000-0000000000oid@00000000-0000-0000-0000-00000000tenant"


def test_cowork_scope_is_the_send_resource():
    # The chat send (/v1/messages) rides the Cowork agent's own resource token.
    assert auth.SCOPES[auth.SCOPE_COWORK] == f"{auth.COWORK_RESOURCE}/.default"


def test_token_endpoint_is_tenant_scoped():
    ep = auth.token_endpoint("TENANT")
    assert ep == "https://login.microsoftonline.com/TENANT/oauth2/v2.0/token"


def test_load_without_refresh_token_raises(tmp_path):
    p = tmp_path / "m365_token.json"
    p.write_text('{"tenant":"t","oid":"o"}', encoding="utf-8")
    try:
        auth.M365Auth.load(str(p))
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "refresh token" in str(e).lower()


def test_all_scopes_are_default_grants():
    # Every known scope is a public-client <resource>/.default string.
    for name, scope in auth.SCOPES.items():
        assert scope.endswith("/.default"), f"{name} -> {scope}"
