import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from db import connection
from main import app


def test_connection_posts_rules_and_comment_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("INSTAGRAM_APP_ID", "test-app")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "test-secret")
    monkeypatch.setenv("INSTAGRAM_REDIRECT_URI", "http://localhost:8000/api/auth/instagram/callback")
    monkeypatch.setenv("INSTAGRAM_WEBHOOK_VERIFY_TOKEN", "verify-me")

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/connection").json() == {"connected": False}
        start = client.get("/api/auth/instagram/start", follow_redirects=False)
        assert start.status_code == 302
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        token = {
            "ig_user_id": "ig-1",
            "access_token": "private-token",
            "token_expires_at": "2099-01-01T00:00:00+00:00",
        }
        with patch("main.exchange_code", new_callable=AsyncMock, return_value=token), patch(
            "main.get_profile", new_callable=AsyncMock, return_value={"username": "creator"}
        ), patch("main.subscribe_to_webhooks", new_callable=AsyncMock):
            callback = client.get(
                "/api/auth/instagram/callback", params={"code": "one-use", "state": state}, follow_redirects=False
            )
        assert callback.status_code == 303
        assert "private-token" not in callback.headers["location"]
        assert client.get("/api/connection").json()["username"] == "creator"
        assert client.get(
            "/api/auth/instagram/callback", params={"code": "one-use", "state": state}, follow_redirects=False
        ).headers["location"].find("error=") != -1

        sample_post = {"id": "post-1", "caption": "A post", "media_type": "IMAGE"}
        with patch("main.get_posts", new_callable=AsyncMock, return_value=[sample_post]):
            assert client.get("/api/posts").json()["data"] == [sample_post]
            assert client.put("/api/automations/post-1", json={"keyword": "hello", "text": "Thanks!"}).status_code == 403
            saved = client.put(
                "/api/automations/post-1",
                json={"keyword": "hello", "text": "Thanks!"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert saved.status_code == 200
            draft_input = {
                "triggers": ["comment"], "check_subscription": True,
                "comment_mode": "keywords", "keywords": ["hello", "guide"],
                "exact_match": False, "post_scope": "selected", "post_ids": ["post-1"],
                "welcome_message": "Welcome!", "welcome_button": "Get guide",
                "welcome_image": None, "no_subscription_message": "Please follow",
                "no_subscription_button": "Done", "after_subscription_message": "Here is the guide",
            }
            assert client.post("/api/automation-drafts", json=draft_input).status_code == 403
            draft = client.post(
                "/api/automation-drafts", json=draft_input,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert draft.status_code == 201
            assert draft.json()["status"] == "active"
            assert draft.json()["enabled"] is True
            assert draft.json()["config"]["keywords"] == ["hello", "guide"]
            assert client.get("/api/automation-drafts").json()["data"][0]["id"] == draft.json()["id"]
            assert client.post(
                "/api/automation-drafts", json={**draft_input, "post_ids": ["someone-elses-post"]},
                headers={"X-Requested-With": "XMLHttpRequest"},
            ).status_code == 404
            assert client.post(
                "/api/automation-drafts", json={**draft_input, "keywords": []},
                headers={"X-Requested-With": "XMLHttpRequest"},
            ).status_code == 422
            assert client.post(
                "/api/automation-drafts", json={**draft_input, "welcome_image": "data:text/html;base64,PHNjcmlwdD4="},
                headers={"X-Requested-With": "XMLHttpRequest"},
            ).status_code == 422
            direct_draft = client.post(
                "/api/automation-drafts",
                json={**draft_input, "triggers": ["direct"], "post_ids": [], "keywords": [], "check_subscription": False},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert direct_draft.status_code == 201
            assert direct_draft.json()["config"]["triggers"] == ["direct"]
            assert client.patch(
                f"/api/automation-drafts/{draft.json()['id']}/enabled",
                json={"enabled": False},
                headers={"X-Requested-With": "XMLHttpRequest"},
            ).json()["enabled"] is False
            assert client.patch(
                f"/api/automation-drafts/{draft.json()['id']}/enabled",
                json={"enabled": True},
                headers={"X-Requested-With": "XMLHttpRequest"},
            ).json()["enabled"] is True
        assert client.get("/api/automations").json()["data"][0]["keyword"] == "hello"

        assert client.get("/api/webhooks/instagram", params={
            "hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"
        }).text == "12345"
        event = {"entry": [{"id": "ig-1", "changes": [{"field": "comments", "value": {
            "id": "comment-1", "media": {"id": "post-1"}, "text": "Hello there"
        }}]}]}
        body = json.dumps(event).encode()
        signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        headers = {"X-Hub-Signature-256": signature}
        with patch("main.reply_to_comment", new_callable=AsyncMock) as reply:
            assert client.post("/api/webhooks/instagram", content=body, headers=headers).status_code == 200
            assert client.post("/api/webhooks/instagram", content=body, headers=headers).status_code == 200
            reply.assert_awaited_once_with("comment-1", "Thanks!", "private-token")
        with connection() as db:
            assert db.execute("SELECT count(*) FROM processed_comments").fetchone()[0] == 1

        assert client.delete(
            "/api/automations/post-1", headers={"X-Requested-With": "XMLHttpRequest"}
        ).status_code == 200
        assert client.get("/api/automations").json()["data"] == []
        draft_only_event = {"entry": [{"id": "ig-1", "changes": [{"field": "comments", "value": {
            "id": "comment-2", "media": {"id": "post-1"}, "text": "Hello again"
        }}]}]}
        draft_only_body = json.dumps(draft_only_event).encode()
        draft_only_signature = "sha256=" + hmac.new(b"test-secret", draft_only_body, hashlib.sha256).hexdigest()
        with patch("main.reply_to_comment", new_callable=AsyncMock) as reply:
            assert client.post(
                "/api/webhooks/instagram", content=draft_only_body,
                headers={"X-Hub-Signature-256": draft_only_signature},
            ).status_code == 200
            reply.assert_not_awaited()
        assert client.delete(
            "/api/connection", headers={"X-Requested-With": "XMLHttpRequest"}
        ).status_code == 200
        assert client.get("/api/connection").json() == {"connected": False}
        with connection() as db:
            assert db.execute("SELECT count(*) FROM automation_drafts").fetchone()[0] == 2
            assert db.execute("SELECT access_token FROM sessions WHERE ig_user_id = ?", ("ig-1",)).fetchone()[0] is None
        disconnected_event = {"entry": [{"id": "ig-1", "changes": [{"field": "comments", "value": {
            "id": "comment-after-disconnect", "media": {"id": "post-1"}, "text": "Hello again"
        }}]}]}
        disconnected_body = json.dumps(disconnected_event).encode()
        disconnected_signature = "sha256=" + hmac.new(b"test-secret", disconnected_body, hashlib.sha256).hexdigest()
        with patch("main.reply_to_comment", new_callable=AsyncMock) as reply, patch(
            "main.send_direct_message", new_callable=AsyncMock
        ) as message:
            assert client.post(
                "/api/webhooks/instagram", content=disconnected_body,
                headers={"X-Hub-Signature-256": disconnected_signature},
            ).status_code == 200
            reply.assert_not_awaited()
            message.assert_not_awaited()
