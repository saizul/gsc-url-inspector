from __future__ import annotations

import base64
import json
import os
import stat
import threading
import urllib.parse
import urllib.request

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from conftest import TOKEN_URI, resp, token_ok
from gsc_url_inspector.auth import (
    SCOPE_READONLY,
    AuthError,
    CachedToken,
    ClientSecrets,
    InstalledAppCredentials,
    LoopbackReceiver,
    ServiceAccountCredentials,
    StaticTokenCredentials,
    TokenCache,
    cache_key,
    detect_credentials_type,
    load_credentials,
)
from gsc_url_inspector.auth.installed_app import build_authorization_url, pkce_pair
from gsc_url_inspector.auth.service_account import JWT_GRANT_TYPE, build_assertion


def _b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _form(body: bytes) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(body.decode()))


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# -- service account ----------------------------------------------------------------------------------------------


def test_assertion_is_signed_rs256_with_expected_claims(sa_info, rsa_key):
    jwt = build_assertion(
        sa_info["client_email"], sa_info["private_key"], [SCOPE_READONLY], TOKEN_URI, "kid-123", None, 1000
    )
    head, claims, sig = jwt.split(".")
    assert json.loads(_b64d(head)) == {"alg": "RS256", "typ": "JWT", "kid": "kid-123"}
    c = json.loads(_b64d(claims))
    assert c == {"iss": sa_info["client_email"], "scope": SCOPE_READONLY, "aud": TOKEN_URI, "iat": 1000, "exp": 4600}
    rsa_key.public_key().verify(_b64d(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_assertion_includes_subject_for_delegation(sa_info):
    jwt = build_assertion(sa_info["client_email"], sa_info["private_key"], [SCOPE_READONLY], subject="me@example.com")
    assert json.loads(_b64d(jwt.split(".")[1]))["sub"] == "me@example.com"


def test_service_account_exchanges_jwt_and_caches(transport, sa_info, tmp_path):
    transport.queue("POST", TOKEN_URI, token_ok("tok-1"))
    cache = TokenCache(tmp_path / "tokens.json")
    clock = Clock()
    creds = ServiceAccountCredentials(sa_info, transport=transport, cache=cache, clock=clock)
    assert creds.access_token() == "tok-1"
    assert creds.access_token() == "tok-1"  # served from memory
    (call,) = transport.calls("POST", TOKEN_URI)
    form = _form(call["body"])
    assert form["grant_type"] == JWT_GRANT_TYPE
    assert form["assertion"].count(".") == 2
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    # A second instance reads the cached token instead of asking again.
    again = ServiceAccountCredentials(sa_info, transport=transport, cache=cache, clock=clock)
    assert again.access_token() == "tok-1"
    assert len(transport.calls("POST", TOKEN_URI)) == 1


def test_service_account_refreshes_when_expired(transport, sa_info):
    transport.queue("POST", TOKEN_URI, token_ok("tok-1", 3600), token_ok("tok-2", 3600))
    clock = Clock()
    creds = ServiceAccountCredentials(sa_info, transport=transport, clock=clock)
    assert creds.access_token() == "tok-1"
    clock.t += 3600 - 30  # inside the expiry skew
    assert creds.access_token() == "tok-2"


def test_service_account_invalidate_drops_cache(transport, sa_info, tmp_path):
    transport.queue("POST", TOKEN_URI, token_ok("a"), token_ok("b"))
    cache = TokenCache(tmp_path / "t.json")
    creds = ServiceAccountCredentials(sa_info, transport=transport, cache=cache)
    assert creds.access_token() == "a"
    creds.invalidate()
    assert creds.access_token() == "b"


def test_service_account_token_error_is_reported(transport, sa_info):
    transport.add("POST", TOKEN_URI, 400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
    creds = ServiceAccountCredentials(sa_info, transport=transport)
    with pytest.raises(AuthError, match="invalid_grant Invalid JWT Signature"):
        creds.access_token()


def test_service_account_network_error(transport, sa_info):
    transport.error("POST", TOKEN_URI)
    with pytest.raises(AuthError, match="unreachable"):
        ServiceAccountCredentials(sa_info, transport=transport).access_token()


def test_service_account_validation(sa_info):
    with pytest.raises(AuthError, match="client_email"):
        ServiceAccountCredentials({**sa_info, "client_email": ""})
    with pytest.raises(AuthError, match="type"):
        ServiceAccountCredentials({**sa_info, "type": "authorized_user"})
    bad = ServiceAccountCredentials({**sa_info, "private_key": "not a key"})
    with pytest.raises(AuthError, match="PEM"):
        bad.access_token()


def test_token_response_without_access_token(transport, sa_info):
    transport.add("POST", TOKEN_URI, 200, {"expires_in": 10})
    with pytest.raises(AuthError, match="no access_token"):
        ServiceAccountCredentials(sa_info, transport=transport).access_token()


# -- token cache --------------------------------------------------------------------------------------------------


def test_token_cache_round_trip_and_permissions(tmp_path):
    cache = TokenCache(tmp_path / "sub" / "tokens.json")
    cache.put("k", CachedToken("access", 2e9, "refresh", scope="s"))
    got = cache.get("k")
    assert got == CachedToken("access", 2e9, "refresh", "Bearer", "s")
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(cache.path).st_mode) == 0o600
    assert cache.delete("k") is True
    assert cache.delete("k") is False
    assert cache.get("k") is None


def test_token_cache_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json")
    cache = TokenCache(path)
    assert cache.get("x") is None
    cache.put("x", CachedToken("a", 1))
    assert cache.get("x").access_token == "a"


def test_cache_key_is_order_independent():
    assert cache_key("id", ["b", "a"]) == cache_key("id", ["a", "b"])
    assert cache_key("id", ["a"]) != cache_key("other", ["a"])


def test_cached_token_validity():
    assert CachedToken("a", 1000).valid(900)
    assert not CachedToken("a", 1000).valid(950)
    assert not CachedToken("", 1e12).valid(0)


# -- installed app ------------------------------------------------------------------------------------------------

SECRETS = {
    "installed": {
        "client_id": "abc.apps.googleusercontent.com",
        "client_secret": "not-a-real-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": TOKEN_URI,
        "redirect_uris": ["http://localhost"],
    }
}


def test_client_secrets_parsing():
    cs = ClientSecrets.from_dict(SECRETS)
    assert cs.client_id.startswith("abc")
    assert cs.token_uri == TOKEN_URI
    with pytest.raises(AuthError):
        ClientSecrets.from_dict({"installed": {}})


def test_pkce_pair_matches_s256():
    import hashlib

    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorization_url_parameters():
    cs = ClientSecrets.from_dict(SECRETS)
    url = build_authorization_url(cs, "http://127.0.0.1:5555/", [SCOPE_READONLY], "st", "ch")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["response_type"] == "code"
    assert q["redirect_uri"] == "http://127.0.0.1:5555/"
    assert q["code_challenge_method"] == "S256"
    assert q["access_type"] == "offline"
    assert q["scope"] == SCOPE_READONLY


def test_exchange_code_and_refresh(transport, tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    clock = Clock()
    transport.queue(
        "POST",
        TOKEN_URI,
        token_ok("first", 3600, refresh_token="refresh-1"),
        token_ok("second", 3600),
    )
    creds = InstalledAppCredentials(ClientSecrets.from_dict(SECRETS), transport=transport, cache=cache, clock=clock)
    tok = creds.exchange_code("the-code", "http://127.0.0.1:1/", "verifier")
    assert tok.refresh_token == "refresh-1"
    form = _form(transport.requests[0]["body"])
    assert form["grant_type"] == "authorization_code"
    assert form["code_verifier"] == "verifier"
    assert form["client_secret"] == "not-a-real-secret"
    assert creds.access_token() == "first"
    clock.t += 7200
    # New process: only the cache is available.
    fresh = InstalledAppCredentials(ClientSecrets.from_dict(SECRETS), transport=transport, cache=cache, clock=clock)
    assert fresh.has_refresh_token()
    assert fresh.access_token() == "second"
    refresh_form = _form(transport.requests[1]["body"])
    assert refresh_form == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": SECRETS["installed"]["client_id"],
        "client_secret": "not-a-real-secret",
    }
    # The refresh token survives a refresh response that does not repeat it.
    assert cache.get(fresh.key).refresh_token == "refresh-1"


def test_installed_app_without_login_fails(transport, tmp_path):
    creds = InstalledAppCredentials(
        ClientSecrets.from_dict(SECRETS), transport=transport, cache=TokenCache(tmp_path / "t.json")
    )
    assert not creds.has_refresh_token()
    with pytest.raises(AuthError, match="auth login"):
        creds.access_token()


def test_invalidate_keeps_refresh_token(transport, tmp_path):
    cache = TokenCache(tmp_path / "t.json")
    transport.queue("POST", TOKEN_URI, token_ok("a", refresh_token="r"), token_ok("b"))
    creds = InstalledAppCredentials(ClientSecrets.from_dict(SECRETS), transport=transport, cache=cache)
    creds.exchange_code("c", "http://127.0.0.1:1/", "v")
    creds.invalidate()
    assert creds.access_token() == "b"
    assert creds.logout() is True


def test_loopback_receiver_captures_code():
    receiver = LoopbackReceiver()
    url = receiver.redirect_uri + "?state=xyz&code=4%2Fabc"

    def hit() -> None:
        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.status == 200

    t = threading.Thread(target=hit)
    t.start()
    params = receiver.wait(timeout=10)
    t.join()
    assert params == {"state": "xyz", "code": "4/abc"}


def test_login_flow_end_to_end(transport, tmp_path):
    transport.queue("POST", TOKEN_URI, token_ok("tok", refresh_token="ref"))
    creds = InstalledAppCredentials(
        ClientSecrets.from_dict(SECRETS), transport=transport, cache=TokenCache(tmp_path / "t.json")
    )
    receiver = LoopbackReceiver()
    printed: list[str] = []

    def printer(msg: str) -> None:
        printed.append(msg)
        auth_url = msg.split("\n\n  ", 1)[1].strip()
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(auth_url).query))
        redirect = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "the-code", "state": q["state"]})
        threading.Thread(target=lambda: urllib.request.urlopen(redirect, timeout=5).read()).start()

    tok = creds.login(open_browser=False, printer=printer, receiver=receiver, timeout=10)
    assert tok.access_token == "tok"
    assert _form(transport.requests[0]["body"])["code"] == "the-code"


def test_login_rejects_state_mismatch(transport, tmp_path):
    creds = InstalledAppCredentials(
        ClientSecrets.from_dict(SECRETS), transport=transport, cache=TokenCache(tmp_path / "t.json")
    )
    receiver = LoopbackReceiver()

    def printer(msg: str) -> None:
        bad = receiver.redirect_uri + "?code=x&state=forged"
        threading.Thread(target=lambda: urllib.request.urlopen(bad, timeout=5).read()).start()

    with pytest.raises(AuthError, match="state mismatch"):
        creds.login(open_browser=False, printer=printer, receiver=receiver, timeout=10)
    assert transport.requests == []


# -- loading --------------------------------------------------------------------------------------------------------


def test_detect_and_load(tmp_path, sa_info, transport):
    assert detect_credentials_type(sa_info) == "service_account"
    assert detect_credentials_type(SECRETS) == "oauth"
    with pytest.raises(AuthError):
        detect_credentials_type({"foo": 1})
    sa_path = tmp_path / "sa.json"
    sa_path.write_text(json.dumps(sa_info))
    oauth_path = tmp_path / "client.json"
    oauth_path.write_text(json.dumps(SECRETS))
    cache = TokenCache(tmp_path / "t.json")
    assert isinstance(load_credentials(sa_path, transport=transport, cache=cache), ServiceAccountCredentials)
    assert isinstance(load_credentials(oauth_path, transport=transport, cache=cache), InstalledAppCredentials)
    with pytest.raises(AuthError, match="cannot read"):
        load_credentials(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("[")
    with pytest.raises(AuthError, match="not valid JSON"):
        load_credentials(tmp_path / "bad.json")


def test_static_token():
    creds = StaticTokenCredentials(" abc \n")
    assert creds.access_token() == "abc"
    creds.invalidate()
    with pytest.raises(AuthError):
        creds.access_token()
    with pytest.raises(AuthError):
        StaticTokenCredentials("  ")


def test_token_error_without_json(transport, sa_info):
    transport.add("POST", TOKEN_URI, 502, "Bad gateway")
    with pytest.raises(AuthError, match="HTTP 502"):
        ServiceAccountCredentials(sa_info, transport=transport).access_token()


def test_resp_helper_smoke():
    assert resp(204).ok
