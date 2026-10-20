from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from db import connection
from instagram import InstagramError
from main import app


HEADERS = {"X-Requested-With": "XMLHttpRequest", "Origin": "http://localhost:3000"}
PROFILE = {"id": "app-scoped-id", "user_id": "professional-id", "username": "creator"}
CONFIG = {
    "triggers": ["direct"], "check_subscription": False,
    "comment_mode": "keywords", "keywords": ["guide"], "exact_match": True,
    "post_scope": "selected", "post_ids": [], "welcome_message": "Here is your guide",
    "welcome_button": "", "no_subscription_message": "", "no_subscription_button": "",
    "after_subscription_message": "", "after_subscription_button": "", "after_subscription_url": "",
}


def test_existing_token_connects_without_cookies_and_preserves_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bridge.db"))
    with TestClient(app) as client:
        with connection() as db:
            db.execute(
                "INSERT INTO sessions (id, ig_user_id, username, access_token, connected_at) VALUES (?, ?, ?, ?, ?)",
                ("old-session", "professional-id", "creator", "old-token", "2026-01-01"),
            )
            db.execute(
                "INSERT INTO automations (id, session_id, post_id, keyword, text, enabled, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
                ("old-rule", "old-session", "post-1", "guide", "A reply", "2026-01-01"),
            )

        with patch("main.get_profile", new_callable=AsyncMock, return_value=PROFILE) as profile, patch(
            "main.subscribe_to_webhooks", new_callable=AsyncMock
        ) as subscribe:
            result = client.post("/api/connection", json={"access_token": "valid-token"}, headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["sessionToken"] == "old-session"
        assert result.json()["instagramUserId"] == "professional-id"
        assert "valid-token" not in result.text
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["access-control-allow-origin"] == HEADERS["Origin"]
        profile.assert_awaited_once_with("valid-token")
        subscribe.assert_awaited_once_with("professional-id", "valid-token")
        client.cookies.clear()
        headers = {**HEADERS, "Authorization": "Bearer old-session"}
        assert client.get("/api/automations", headers=headers).json()["data"][0]["id"] == "old-rule"

        migrated = client.post("/api/automation-drafts?replace_automation_id=old-rule", json=CONFIG, headers=headers)
        assert migrated.status_code == 201
        assert migrated.json()["enabled"] is False
        assert client.get("/api/automations", headers=headers).json()["data"] == []
        assert client.get("/api/automation-drafts", headers=headers).json()["data"][0]["config"]["exact_match"] is True
        draft_id = migrated.json()["id"]
        assert client.patch(f"/api/automation-drafts/{draft_id}/enabled", json={"enabled": True}, headers=headers).json()["enabled"] is True
        assert client.put(f"/api/automation-drafts/{draft_id}", json={**CONFIG, "welcome_message": "Updated"}, headers=headers).json()["config"]["welcome_message"] == "Updated"
        with connection() as db:
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
            assert db.execute("SELECT access_token FROM sessions").fetchone()[0] == "valid-token"


def test_bridge_rejects_invalid_tokens_origins_and_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "invalid.db"))
    with TestClient(app) as client:
        assert client.post("/api/connection", json={"access_token": "bad"}).status_code == 403
        assert client.post("/api/connection", json={"access_token": "bad"}, headers={**HEADERS, "Origin": "https://untrusted.example"}).status_code == 403
        with patch("main.get_profile", new_callable=AsyncMock, side_effect=InstagramError("Invalid token")):
            assert client.post("/api/connection", json={"access_token": "bad"}, headers=HEADERS).status_code == 401
        assert client.get("/api/automation-drafts", headers={"Authorization": "Bearer nonexistent"}).status_code == 401
        assert client.get("/api/automation-drafts", headers={"Authorization": "Basic invalid"}).status_code == 401
        with connection() as db:
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_migration_cannot_replace_another_accounts_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ownership.db"))
    with TestClient(app) as client:
        with connection() as db:
            for session_id, account_id in [("owner", "account-1"), ("other", "account-2")]:
                db.execute("INSERT INTO sessions (id, ig_user_id, access_token) VALUES (?, ?, ?)", (session_id, account_id, "token"))
            db.execute("INSERT INTO automations (id, session_id, post_id, keyword, text, updated_at) VALUES (?, ?, ?, ?, ?, ?)", ("other-rule", "other", "post", "hi", "reply", "2026-01-01"))
        headers = {**HEADERS, "Authorization": "Bearer owner"}
        assert client.post("/api/automation-drafts?replace_automation_id=other-rule", json=CONFIG, headers=headers).status_code == 404
        with connection() as db:
            assert db.execute("SELECT COUNT(*) FROM automation_drafts").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM automations").fetchone()[0] == 1


def test_client_exchange_uses_the_registered_callback_and_rejects_other_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "exchange.db"))
    redirect = "http://localhost:3000/social-automation"
    token = {"access_token": "verified-token", "ig_user_id": "professional-id"}
    with TestClient(app) as client, patch("main.exchange_code", new_callable=AsyncMock, return_value=token) as exchange:
        result = client.post("/api/auth/instagram/exchange", json={"code": "one-use-code", "redirect_uri": redirect}, headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["accessToken"] == "verified-token"
        assert result.headers["cache-control"] == "no-store"
        exchange.assert_awaited_once_with("one-use-code", redirect)
        for invalid in ["https://untrusted.example/social-automation", "http://localhost:3000/another-page", redirect + "?untrusted=1"]:
            assert client.post("/api/auth/instagram/exchange", json={"code": "code", "redirect_uri": invalid}, headers=HEADERS).status_code == 403
        assert exchange.await_count == 1


def test_backend_oauth_hands_off_one_use_session_to_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "handoff.db"))
    monkeypatch.setenv("INSTAGRAM_APP_ID", "test-app")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "test-secret")
    monkeypatch.setenv("INSTAGRAM_REDIRECT_URI", "http://localhost:8000/api/auth/instagram/callback")
    return_to = "http://localhost:3000/social-automation"
    token = {"access_token": "private-token", "ig_user_id": "professional-id", "token_expires_at": "2099-01-01T00:00:00+00:00"}

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/auth/instagram/start", params={"return_to": "https://untrusted.example/social-automation"}).status_code == 400
        start = client.get("/api/auth/instagram/start", params={"return_to": return_to}, follow_redirects=False)
        assert start.status_code == 302
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        with patch("main.exchange_code", new_callable=AsyncMock, return_value=token), patch(
            "main.get_profile", new_callable=AsyncMock, return_value={"user_id": "professional-id", "username": "creator"}
        ), patch("main.subscribe_to_webhooks", new_callable=AsyncMock):
            callback = client.get("/api/auth/instagram/callback", params={"code": "one-use", "state": state}, follow_redirects=False)
        assert callback.status_code == 303
        assert callback.headers["location"].startswith(return_to + "?sa_ticket=")
        assert "private-token" not in callback.headers["location"]
        ticket = parse_qs(urlparse(callback.headers["location"]).query)["sa_ticket"][0]
        assert client.post("/api/auth/instagram/claim", json={"ticket": ticket}).status_code == 403
        result = client.post("/api/auth/instagram/claim", json={"ticket": ticket}, headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["instagramUserId"] == "professional-id"
        assert "private-token" not in result.text
        assert result.headers["cache-control"] == "no-store"
        assert client.post("/api/auth/instagram/claim", json={"ticket": ticket}, headers=HEADERS).status_code == 400
        client.cookies.clear()
        assert client.get("/api/connection", headers={"Authorization": f"Bearer {result.json()['sessionToken']}"}).json()["connected"] is True
        retry = client.get("/api/auth/instagram/start", params={"return_to": return_to}, follow_redirects=False)
        retry_state = parse_qs(urlparse(retry.headers["location"]).query)["state"][0]
        client.cookies.clear()
        missing_cookie = client.get("/api/auth/instagram/callback", params={"code": "one-use", "state": retry_state}, follow_redirects=False)
        assert missing_cookie.headers["location"].startswith(return_to + "?error=")
