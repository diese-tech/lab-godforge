"""
Integration tests for the web admin API bridge.

These tests exercise the HTTP surface directly with temporary JSON files and no
real Discord connection.
"""

import json
import sys
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

from utils import ledger as ledger_utils
from utils import wallet as wallet_utils
from utils import audit as audit_utils
from utils import dashboard_store
from web_api import server as web_server

_Session = namedtuple("_Session", ["cookie", "csrf"])


def _start_server():
    httpd = web_server.create_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _stop_server(httpd):
    httpd.shutdown()
    httpd.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(method, url, payload=None, cookie=None, follow_redirects=True):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if isinstance(cookie, _Session):
        request.add_header("Cookie", cookie.cookie)
        request.add_header("X-CSRF-Token", cookie.csrf)
    elif cookie:
        request.add_header("Cookie", cookie)

    opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(_NoRedirect)

    try:
        with opener.open(request, timeout=3) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return response.status, parsed, response.headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed, exc.headers


def _parse_set_cookies(headers) -> dict[str, str]:
    """Extract all Set-Cookie values keyed by cookie name."""
    cookies = {}
    for value in headers.get_all("Set-Cookie") or []:
        name_val = value.split(";", 1)[0].strip()
        if "=" in name_val:
            name, val = name_val.split("=", 1)
            cookies[name.strip()] = val.strip()
    return cookies


def _login(base):
    status, payload, headers = _request(
        "POST",
        f"{base}/api/auth/login",
        {"password": "secret-test"},
    )
    assert status == 200
    assert payload["authenticated"] is True
    cookies = _parse_set_cookies(headers)
    session_val = cookies[web_server.SESSION_COOKIE]
    csrf_val = cookies[web_server.CSRF_COOKIE]
    return _Session(
        cookie=f"{web_server.SESSION_COOKIE}={session_val}; {web_server.CSRF_COOKIE}={csrf_val}",
        csrf=csrf_val,
    )


def test_public_health_and_tool_endpoints_do_not_require_auth(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    httpd, base = _start_server()
    try:
        for path in (
            "/api/health",
            "/api/gods/roll?role=jungle&source=website",
            "/api/gods/roll5?role=jungle&source=website",
            "/api/builds/roll?role=adc&type=standard&count=6",
        ):
            status, payload, _ = _request("GET", f"{base}{path}")
            assert status == 200
            assert payload["ok"] is True
    finally:
        _stop_server(httpd)


def test_protected_admin_endpoints_reject_unauthenticated_writes(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    httpd, base = _start_server()
    try:
        status, payload, _ = _request(
            "POST",
            f"{base}/api/match/create",
            {"team1": "Solaris", "team2": "Onyx"},
        )

        assert status == 401
        assert "login" in payload["error"].lower()
        assert ledger_utils.load_ledger()["matches"] == []
    finally:
        _stop_server(httpd)


def test_legacy_economy_mutation_endpoints_are_blocked_without_legacy_flag(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    monkeypatch.setattr(web_server, "LEGACY_ECONOMY_ENABLED", False)
    httpd, base = _start_server()
    try:
        cookie = _login(base)
        status, payload, _ = _request(
            "POST",
            f"{base}/api/match/create",
            {"team1": "Solaris", "team2": "Onyx"},
            cookie,
        )

        assert status == 400
        assert "disabled" in payload["error"].lower()
        assert ledger_utils.load_ledger()["matches"] == []
    finally:
        _stop_server(httpd)


def test_wrong_password_does_not_unlock_admin_endpoints(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    httpd, base = _start_server()
    try:
        status, payload, headers = _request(
            "POST",
            f"{base}/api/auth/login",
            {"password": "wrong"},
        )
        assert status == 401
        assert "Set-Cookie" not in headers

        status, payload, _ = _request("GET", f"{base}/api/wallets")
        assert status == 401
    finally:
        _stop_server(httpd)


def test_auth_status_reports_discord_oauth_configuration(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
    monkeypatch.delenv("DISCORD_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("DISCORD_OAUTH_REDIRECT_URI", raising=False)
    httpd, base = _start_server()
    try:
        status, payload, _ = _request("GET", f"{base}/api/auth/status")
        assert status == 200
        assert payload["discordOAuthConfigured"] is False
    finally:
        _stop_server(httpd)

    monkeypatch.setenv("DISCORD_CLIENT_ID", "1493371999031136318")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "oauth-secret")
    monkeypatch.setenv("DISCORD_OAUTH_REDIRECT_URI", "https://godforge-hub.up.railway.app/api/auth/discord/callback")
    httpd, base = _start_server()
    try:
        status, payload, _ = _request("GET", f"{base}/api/auth/status")
        assert status == 200
        assert payload["discordOAuthConfigured"] is True
    finally:
        _stop_server(httpd)


def test_discord_oauth_env_values_are_trimmed(monkeypatch):
    monkeypatch.setenv("DISCORD_CLIENT_ID", " 1493371999031136318 ")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", " oauth-secret ")
    monkeypatch.setenv("DISCORD_OAUTH_REDIRECT_URI", " https://godforge-hub.up.railway.app/api/auth/discord/callback ")

    assert web_server._discord_client_id() == "1493371999031136318"
    assert web_server._discord_client_secret() == "oauth-secret"
    assert web_server._discord_redirect_uri() == "https://godforge-hub.up.railway.app/api/auth/discord/callback"
    assert web_server._discord_oauth_configured() is True


def test_discord_oauth_exchange_uses_aiohttp_form_client(monkeypatch):
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1493371999031136318")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "oauth-secret")
    monkeypatch.setenv("DISCORD_OAUTH_REDIRECT_URI", "https://godforge-hub.up.railway.app/api/auth/discord/callback")
    captured = {}

    def fake_post_form(path, data):
        captured["path"] = path
        captured["data"] = data
        return "pending-request"

    def fake_run(request):
        assert request == "pending-request"
        return {"access_token": "token-abc"}

    monkeypatch.setattr(web_server, "_discord_post_form", fake_post_form)
    monkeypatch.setattr(web_server, "_run_discord_request", fake_run)

    token = web_server._exchange_discord_code("abc")

    assert token == {"access_token": "token-abc"}
    assert captured["path"] == "/oauth2/token"
    assert captured["data"] == {
        "client_id": "1493371999031136318",
        "client_secret": "oauth-secret",
        "grant_type": "authorization_code",
        "code": "abc",
        "redirect_uri": "https://godforge-hub.up.railway.app/api/auth/discord/callback",
    }


def test_discord_oauth_start_redirects_with_signed_state(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1493371999031136318")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "oauth-secret")
    monkeypatch.setenv("DISCORD_OAUTH_REDIRECT_URI", "https://godforge-hub.up.railway.app/api/auth/discord/callback")
    httpd, base = _start_server()
    try:
        status, payload, headers = _request("GET", f"{base}/api/auth/discord/start", follow_redirects=False)
        location = headers["Location"]
        parsed = urllib.parse.urlparse(location)
        query = urllib.parse.parse_qs(parsed.query)
        state_cookie = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]

        assert status == 302
        assert parsed.netloc == "discord.com"
        assert query["client_id"] == ["1493371999031136318"]
        assert query["redirect_uri"] == ["https://godforge-hub.up.railway.app/api/auth/discord/callback"]
        assert query["scope"] == ["identify guilds"]
        assert web_server._verify_oauth_state(query["state"][0], state_cookie)
    finally:
        _stop_server(httpd)


def test_discord_oauth_callback_rejects_bad_state(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1493371999031136318")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "oauth-secret")
    monkeypatch.setenv("DISCORD_OAUTH_REDIRECT_URI", "https://godforge-hub.up.railway.app/api/auth/discord/callback")
    httpd, base = _start_server()
    try:
        status, payload, headers = _request(
            "GET",
            f"{base}/api/auth/discord/callback?code=abc&state=bad",
            cookie=f"{web_server.OAUTH_STATE_COOKIE}=also-bad",
            follow_redirects=False,
        )

        assert status == 302
        assert "failed:invalid_oauth_state" in headers["Location"]
    finally:
        _stop_server(httpd)


def test_discord_oauth_callback_sets_admin_session(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1493371999031136318")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "oauth-secret")
    monkeypatch.setenv("DISCORD_OAUTH_REDIRECT_URI", "https://godforge-hub.up.railway.app/api/auth/discord/callback")
    monkeypatch.setattr(web_server, "_exchange_discord_code", lambda code: {"access_token": f"token-{code}"})
    monkeypatch.setattr(web_server, "_fetch_discord_user", lambda token: {"id": "42", "username": "AtlasMain"})
    state = "state-test"
    state_cookie = web_server._sign_oauth_state(state)
    httpd, base = _start_server()
    try:
        status, payload, headers = _request(
            "GET",
            f"{base}/api/auth/discord/callback?code=abc&state={state}",
            cookie=f"{web_server.OAUTH_STATE_COOKIE}={state_cookie}",
            follow_redirects=False,
        )
        session_cookie = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]

        assert status == 302
        assert headers["Location"] == "/#dashboard?auth=discord"
        assert web_server._verify_session(session_cookie)
    finally:
        _stop_server(httpd)


def test_discord_oauth_session_unlocks_protected_endpoints_without_admin_password(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.delenv("GODFORGE_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1493371999031136318")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "oauth-secret")
    monkeypatch.setenv("DISCORD_OAUTH_REDIRECT_URI", "https://godforge-hub.up.railway.app/api/auth/discord/callback")
    monkeypatch.setattr(web_server, "_exchange_discord_code", lambda code: {"access_token": f"token-{code}"})
    monkeypatch.setattr(web_server, "_fetch_discord_user", lambda token: {"id": "42", "username": "AtlasMain"})
    state = "state-test"
    state_cookie = web_server._sign_oauth_state(state)
    httpd, base = _start_server()
    try:
        status, _payload, headers = _request(
            "GET",
            f"{base}/api/auth/discord/callback?code=abc&state={state}",
            cookie=f"{web_server.OAUTH_STATE_COOKIE}={state_cookie}",
            follow_redirects=False,
        )
        session_cookie = headers["Set-Cookie"].split(";", 1)[0]

        assert status == 302
        assert headers["Location"] == "/#dashboard?auth=discord"

        status, auth_status, _ = _request("GET", f"{base}/api/auth/status", cookie=session_cookie)
        assert status == 200
        assert auth_status["authenticated"] is True
        assert auth_status["configured"] is False
        assert auth_status["discordOAuthConfigured"] is True

        status, admin_status, _ = _request("GET", f"{base}/api/admin/status", cookie=session_cookie)
        assert status == 200
        assert admin_status["ok"] is True
    finally:
        _stop_server(httpd)


def test_tampered_admin_cookie_is_rejected(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    httpd, base = _start_server()
    try:
        cookie = f"{web_server.SESSION_COOKIE}=not-a-real-session"

        status, payload, _ = _request("GET", f"{base}/api/admin/status", cookie=cookie)

        assert status == 401
        assert "login" in payload["error"].lower()
    finally:
        _stop_server(httpd)


def test_static_file_lookup_rejects_path_traversal():
    assert web_server._static_path("/index.html").name == "index.html"
    assert web_server._static_path("/../bot.py") is None
    assert web_server._static_path("/..%2Fbot.py") is None


def test_admin_status_reports_data_counts_and_requires_auth(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    ledger_utils.create_match("Solaris", "Onyx")
    wallet_utils.ensure_wallet(111, "AtlasMain")
    httpd, base = _start_server()
    try:
        status, payload, _ = _request("GET", f"{base}/api/admin/status")
        assert status == 401

        cookie = _login(base)
        status, payload, _ = _request("GET", f"{base}/api/admin/status", cookie=cookie)

        assert status == 200
        assert payload["status"]["data"]["matchCount"] == 1
        assert payload["status"]["data"]["walletCount"] == 1
        assert payload["status"]["data"]["statusCounts"]["betting_open"] == 1
        assert payload["status"]["bot"]["guildCount"] >= 0
        modules = {module["key"]: module for module in payload["status"]["modules"]}
        assert modules["randomizer"]["state"] == "ready"
        assert modules["betting-wallets"]["state"] == "needs_setup"
        assert "Discord ledger embed" in modules["betting-wallets"]["needs"]
    finally:
        _stop_server(httpd)


def test_admin_status_module_health_reflects_saved_settings(monkeypatch, tmp_ledger, tmp_wallets, tmp_settings):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    ledger_utils.update_embed_info(12345, 67890)
    httpd, base = _start_server()
    try:
        cookie = _login(base)
        status, _, _ = _request(
            "POST",
            f"{base}/api/settings",
            {
                "guild_id": "global",
                "features": {"draftsEnabled": False, "bettingEnabled": True},
                "channels": {"matchChannel": "#matches", "bettingChannel": "#bets", "adminChannel": "#admin"},
                "roles": {"adminRole": "Admins", "captainRole": "Captains"},
            },
            cookie,
        )
        assert status == 200

        status, payload, _ = _request("GET", f"{base}/api/admin/status", cookie=cookie)
        modules = {module["key"]: module for module in payload["status"]["modules"]}

        assert status == 200
        assert modules["match-ops"]["state"] == "ready"
        assert modules["betting-wallets"]["state"] == "ready"
        assert modules["drafts"]["enabled"] is False
        assert modules["settings"]["state"] == "ready"
    finally:
        _stop_server(httpd)


def test_admin_status_reports_limited_guild_telemetry(monkeypatch, tmp_ledger, tmp_wallets, tmp_settings):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")

    class FakeGuild:
        def __init__(self, index):
            self.id = index
            self.name = f"Guild {index}"

    fake_client = types.SimpleNamespace(
        is_ready=lambda: True,
        user="GodForge#5322",
        guilds=[FakeGuild(index) for index in range(30)],
        latency=0.123,
    )
    fake_bot = types.SimpleNamespace(client=fake_client)
    monkeypatch.setitem(sys.modules, "bot", fake_bot)

    httpd, base = _start_server()
    try:
        cookie = _login(base)
        status, payload, _ = _request("GET", f"{base}/api/admin/status", cookie=cookie)
        bot = payload["status"]["bot"]

        assert status == 200
        assert bot["connected"] is True
        assert bot["user"] == "GodForge#5322"
        assert bot["guildCount"] == 30
        assert len(bot["guilds"]) == 25
        assert bot["latencyMs"] == 123
    finally:
        _stop_server(httpd)


def test_manual_ledger_sync_is_protected_and_schedules_refresh(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    calls = []
    monkeypatch.setattr(web_server, "_schedule_ledger_embed_refresh", lambda: calls.append("refresh") or True)
    httpd, base = _start_server()
    try:
        status, payload, _ = _request("POST", f"{base}/api/admin/sync/ledger", {})
        assert status == 401

        cookie = _login(base)
        status, payload, _ = _request("POST", f"{base}/api/admin/sync/ledger", {}, cookie)

        assert status == 200
        assert payload["discord_embed_update"] is True
        assert calls == ["refresh"]
    finally:
        _stop_server(httpd)


def test_settings_read_write_requires_auth_and_persists(monkeypatch, tmp_ledger, tmp_wallets, tmp_settings):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    httpd, base = _start_server()
    try:
        status, payload, _ = _request("GET", f"{base}/api/settings?guild_id=global")
        assert status == 401

        cookie = _login(base)
        update = {
            "guild_id": "global",
            "updated_by": "test-admin",
            "features": {"botEnabled": True, "draftsEnabled": False, "unknown": False},
            "channels": {"matchChannel": "#matches", "bettingChannel": "#bets"},
            "roles": {"adminRole": "Admins", "captainRole": "Captains"},
            "permissions": {"monetizeAccess": "read"},
        }
        status, saved, _ = _request("POST", f"{base}/api/settings", update, cookie)
        assert status == 200
        assert saved["settings"]["features"]["draftsEnabled"] is False
        assert saved["settings"]["channels"]["matchChannel"] == "#matches"
        assert "unknown" not in saved["settings"]["features"]

        status, loaded, _ = _request("GET", f"{base}/api/settings?guild_id=global", cookie=cookie)
        assert status == 200
        assert loaded["settings"]["roles"]["captainRole"] == "Captains"
        assert loaded["settings"]["permissions"]["monetizeAccess"] == "read"
        assert loaded["settings"]["updated_by"] == "test-admin"
    finally:
        _stop_server(httpd)


def test_dashboard_storage_defaults_to_json(monkeypatch):
    monkeypatch.delenv("GODFORGE_STORAGE", raising=False)

    assert dashboard_store.sqlite_enabled() is False
    assert dashboard_store.storage_status()["kind"] == "json"


def test_sqlite_dashboard_storage_persists_settings_audit_and_commands(
    monkeypatch,
    tmp_ledger,
    tmp_wallets,
    tmp_settings,
    tmp_audit,
    tmp_custom_commands,
    tmp_dashboard_db,
):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    monkeypatch.setenv("GODFORGE_STORAGE", "sqlite")
    httpd, base = _start_server()
    try:
        cookie = _login(base)
        status, saved_settings, _ = _request(
            "POST",
            f"{base}/api/settings",
            {
                "guild_id": "global",
                "channels": {"adminChannel": "#admin"},
                "roles": {"adminRole": "Admins"},
            },
            cookie,
        )
        assert status == 200

        status, saved_command, _ = _request(
            "POST",
            f"{base}/api/commands/custom",
            {
                "guild_id": "global",
                "trigger": ".rules",
                "response": "Read the rules.",
                "role_gate": "Everyone",
            },
            cookie,
        )
        assert status == 200

        status, admin_status, _ = _request("GET", f"{base}/api/admin/status", cookie=cookie)
        status, audit, _ = _request("GET", f"{base}/api/admin/audit", cookie=cookie)
        status, commands, _ = _request("GET", f"{base}/api/commands/custom?guild_id=global", cookie=cookie)

        assert saved_settings["settings"]["roles"]["adminRole"] == "Admins"
        assert saved_command["command"]["trigger"] == ".rules"
        assert admin_status["status"]["storage"]["kind"] == "sqlite"
        assert admin_status["status"]["storage"]["available"] is True
        assert audit["events"][0]["action"] == "commands.upsert"
        assert commands["commands"][0]["trigger"] == ".rules"
        assert tmp_dashboard_db.exists()
        assert not tmp_settings.exists()
        assert not tmp_custom_commands.exists()
    finally:
        _stop_server(httpd)
        monkeypatch.delenv("GODFORGE_STORAGE", raising=False)


def test_settings_rejects_bad_guild_ids_and_control_characters(monkeypatch, tmp_ledger, tmp_wallets, tmp_settings):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    httpd, base = _start_server()
    try:
        cookie = _login(base)
        bad_payloads = [
            {"guild_id": "../secret", "features": {"botEnabled": True}},
            {"guild_id": "global", "channels": {"matchChannel": "#matches\nSet-Cookie: hacked=true"}},
            {"guild_id": "global", "roles": {"adminRole": "A" * 81}},
            {"guild_id": "global", "permissions": {"monetizeAccess": "root"}},
        ]

        for payload in bad_payloads:
            status, response, _ = _request("POST", f"{base}/api/settings", payload, cookie)
            assert status == 400
            assert response["ok"] is False
    finally:
        _stop_server(httpd)


def test_admin_audit_requires_auth_and_records_mutations(monkeypatch, tmp_ledger, tmp_wallets, tmp_settings):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    monkeypatch.setattr(web_server, "LEGACY_ECONOMY_ENABLED", True)
    httpd, base = _start_server()
    try:
        status, payload, _ = _request("GET", f"{base}/api/admin/audit")
        assert status == 401

        cookie = _login(base)
        status, created, _ = _request(
            "POST",
            f"{base}/api/match/create",
            {"team1": "Solaris", "team2": "Onyx"},
            cookie,
        )
        assert status == 200

        status, audit, _ = _request("GET", f"{base}/api/admin/audit?limit=5", cookie=cookie)
        assert status == 200
        assert audit["events"][0]["action"] == "match.create"
        assert audit["events"][0]["target"] == created["match"]["match_id"]
    finally:
        _stop_server(httpd)


def test_custom_command_configs_are_protected_persisted_and_deletable(monkeypatch, tmp_ledger, tmp_wallets, tmp_custom_commands):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    httpd, base = _start_server()
    try:
        status, payload, _ = _request("GET", f"{base}/api/commands/custom?guild_id=global")
        assert status == 401

        cookie = _login(base)
        command = {
            "guild_id": "global",
            "trigger": ".scrimrules",
            "response": "Post lobby code and draft reminder.",
            "channel": "#captains",
            "role_gate": "Captains",
            "cooldown": "15s",
            "enabled": True,
        }
        status, saved, _ = _request("POST", f"{base}/api/commands/custom", command, cookie)
        assert status == 200
        assert saved["command"]["trigger"] == ".scrimrules"

        status, loaded, _ = _request("GET", f"{base}/api/commands/custom?guild_id=global", cookie=cookie)
        assert status == 200
        assert loaded["commands"][0]["role_gate"] == "Captains"

        status, deleted, _ = _request("POST", f"{base}/api/commands/custom/delete", {"guild_id": "global", "trigger": ".scrimrules"}, cookie)
        assert status == 200
        assert deleted["deleted"] is True
        assert deleted["commands"] == []
    finally:
        _stop_server(httpd)


def test_custom_command_configs_reject_conflicts_and_malicious_payloads(monkeypatch, tmp_ledger, tmp_wallets, tmp_custom_commands):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    httpd, base = _start_server()
    try:
        cookie = _login(base)
        bad_payloads = [
            {"guild_id": "global", "trigger": ".rg", "response": "conflict"},
            {"guild_id": "../secret", "trigger": ".safe", "response": "bad guild"},
            {"guild_id": "global", "trigger": ".x", "response": "too short"},
            {"guild_id": "global", "trigger": ".safe", "response": "ok", "role_gate": "Root"},
            {"guild_id": "global", "trigger": ".safe", "response": "A" * 601},
        ]

        for payload in bad_payloads:
            status, response, _ = _request("POST", f"{base}/api/commands/custom", payload, cookie)
            assert status == 400
            assert response["ok"] is False
    finally:
        _stop_server(httpd)


def test_audit_log_sanitizes_and_trims_malicious_metadata(tmp_audit):
    event = audit_utils.record_event(
        "settings.update\nSet-Cookie: hacked=true",
        "<script>alert(1)</script>",
        metadata={"x" * 60: "A" * 140, "nested": {"ignored": True}},
    )

    assert "\n" not in event["action"]
    assert event["target"] == "<script>alert(1)</script>"
    assert list(event["metadata"].keys()) == ["x" * 40]
    assert event["metadata"]["x" * 40] == "A" * 120


def test_admin_mutations_schedule_discord_ledger_refresh(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    monkeypatch.setattr(web_server, "LEGACY_ECONOMY_ENABLED", True)
    calls = []
    monkeypatch.setattr(web_server, "_schedule_ledger_embed_refresh", lambda: calls.append("refresh") or True)
    httpd, base = _start_server()
    try:
        cookie = _login(base)

        status, created, _ = _request(
            "POST",
            f"{base}/api/match/create",
            {"team1": "Solaris", "team2": "Onyx"},
            cookie,
        )
        match_id = created["match"]["match_id"]
        assert status == 200
        assert created["discord_embed_update"] is True

        mutations = [
            ("POST", "/api/bet/place", {"match_id": match_id, "username": "AtlasMain", "type": "win", "team": "Solaris", "amount": 100}),
            ("POST", "/api/match/status", {"match_id": match_id, "status": "in_progress"}),
            ("POST", "/api/match/resolve/winner", {"match_id": match_id, "winner": "Solaris"}),
            ("POST", "/api/ledger/reset", {}),
        ]

        for method, path, payload in mutations:
            status, response, _ = _request(method, f"{base}{path}", payload, cookie)
            assert status == 200
            assert response["discord_embed_update"] is True

        assert len(calls) == 5
    finally:
        _stop_server(httpd)


def test_reset_preserves_embed_pointer_for_in_place_discord_update(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    monkeypatch.setattr(web_server, "LEGACY_ECONOMY_ENABLED", True)
    ledger_utils.create_match("@TeamA", "@TeamB")
    ledger_utils.update_embed_info(12345, 67890)
    httpd, base = _start_server()
    try:
        cookie = _login(base)
        status, payload, _ = _request("POST", f"{base}/api/ledger/reset", {}, cookie)

        data = ledger_utils.load_ledger()
        assert status == 200
        assert payload["cleared"] == 1
        assert data["matches"] == []
        assert data["embed_message_id"] == 12345
        assert data["embed_channel_id"] == 67890
    finally:
        _stop_server(httpd)


def test_malicious_bet_payloads_do_not_debit_wallet_or_write_bets(monkeypatch, tmp_ledger, tmp_wallets):
    monkeypatch.setenv("GODFORGE_ADMIN_PASSWORD", "secret-test")
    match = ledger_utils.create_match("Solaris", "Onyx")
    wallet_utils.seed_wallet(111, "AtlasMain")
    httpd, base = _start_server()
    try:
        cookie = _login(base)

        bad_payloads = [
            {"match_id": match["match_id"], "user_id": 111, "username": "AtlasMain", "type": "win", "team": "Solaris", "amount": -50},
            {"match_id": match["match_id"], "user_id": 111, "username": "AtlasMain", "type": "win", "team": "Intruder", "amount": 50},
            {"match_id": match["match_id"], "user_id": 111, "username": "AtlasMain", "type": "prop", "player": "ADC", "stat": "kills", "direction": "sideways", "threshold": 7.5, "amount": 50},
        ]

        for payload in bad_payloads:
            status, response, _ = _request("POST", f"{base}/api/bet/place", payload, cookie)
            assert status == 400
            assert response["ok"] is False

        assert wallet_utils.get_wallet(111)["balance"] == 500
        assert ledger_utils.get_match(match["match_id"])["bets"] == []
    finally:
        _stop_server(httpd)
