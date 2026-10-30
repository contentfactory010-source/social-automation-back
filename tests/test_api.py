import asyncio
import hashlib
import hmac
import importlib
import json
import sys
from types import ModuleType
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from db import connection
from instagram import InstagramError
from instagram import send_direct_message
from main import app


def test_private_reply_uses_instagram_account_and_comment_id(tmp_path, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"recipient_id": "commenter-1", "message_id": "message-1"})

    original_client = httpx.AsyncClient
    with patch("instagram.httpx.AsyncClient", side_effect=lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs
    )):
        recipient_id = asyncio.run(send_direct_message(
            "ig-1", "", "Please follow", "private-token", "Done", "", "FOLLOW", comment_id="comment-1"
        ))
        asyncio.run(send_direct_message("ig-1", "commenter-1", "Thanks", "private-token"))

    assert recipient_id == "commenter-1"
    assert requests[0].url.path == "/ig-1/messages"
    assert json.loads(requests[0].content)["recipient"] == {"comment_id": "comment-1"}
    assert json.loads(requests[0].content)["message"]["quick_replies"][0]["payload"] == "FOLLOW"
    assert requests[1].url.path == "/me/messages"
    assert json.loads(requests[1].content)["recipient"] == {"id": "commenter-1"}


def test_worker_private_reply_uses_instagram_account_and_comment_id(tmp_path, monkeypatch):
    calls = []

    class Response:
        status = 200

        async def json(self):
            return {"recipient_id": "commenter-1", "message_id": "message-1"}

    async def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    worker_stub = ModuleType("workers")
    worker_stub.fetch = fetch
    with patch.dict(sys.modules, {"workers": worker_stub}):
        try:
            worker_client = importlib.import_module("cloudflare_instagram")
            recipient_id = asyncio.run(worker_client.send_direct_message(
                "ig-1", "", "Please follow", "private-token", "Done", "", "FOLLOW", comment_id="comment-1"
            ))
            asyncio.run(worker_client.send_direct_message("ig-1", "commenter-1", "Thanks", "private-token"))
        finally:
            sys.modules.pop("cloudflare_instagram", None)

    assert recipient_id == "commenter-1"
    assert calls[0][0] == "https://graph.instagram.com/ig-1/messages"
    assert json.loads(calls[0][1]["body"])["recipient"] == {"comment_id": "comment-1"}
    assert calls[1][0] == "https://graph.instagram.com/me/messages"
    assert json.loads(calls[1][1]["body"])["recipient"] == {"id": "commenter-1"}


def test_private_reply_retries_without_interactive_content_on_meta_2534015(tmp_path, monkeypatch):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {
                "message": "Invalid message data", "code": 100, "error_subcode": 2534015,
            }})
        return httpx.Response(200, json={"recipient_id": "commenter-1"})

    original_client = httpx.AsyncClient
    with patch("instagram.httpx.AsyncClient", side_effect=lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs
    )):
        recipient = asyncio.run(send_direct_message(
            "ig-1", "", "Please follow", "private-token", "Done", "", "FOLLOW", comment_id="comment-1"
        ))

    assert recipient == "commenter-1"
    assert calls[0]["message"]["quick_replies"][0]["payload"] == "FOLLOW"
    assert calls[1] == {
        "recipient": {"comment_id": "comment-1"},
        "message": {"text": "Please follow\n\nReply / Ответьте: Done"},
    }


def test_worker_private_reply_retries_without_interactive_content_on_meta_2534015(tmp_path, monkeypatch):
    calls = []

    class Response:
        def __init__(self, status):
            self.status = status

        async def json(self):
            return {"recipient_id": "commenter-1"}

        async def text(self):
            return json.dumps({"error": {
                "message": "Invalid message data", "code": 100, "error_subcode": 2534015,
            }})

    async def fetch(url, **kwargs):
        calls.append(json.loads(kwargs["body"]))
        return Response(400 if len(calls) == 1 else 200)

    worker_stub = ModuleType("workers")
    worker_stub.fetch = fetch
    with patch.dict(sys.modules, {"workers": worker_stub}):
        try:
            worker_client = importlib.import_module("cloudflare_instagram")
            recipient = asyncio.run(worker_client.send_direct_message(
                "ig-1", "", "Open the link", "private-token", "Guide", "https://example.com/guide",
                comment_id="comment-1",
            ))
        finally:
            sys.modules.pop("cloudflare_instagram", None)

    assert recipient == "commenter-1"
    assert calls[0]["message"]["attachment"]["payload"]["template_type"] == "button"
    assert calls[1] == {
        "recipient": {"comment_id": "comment-1"},
        "message": {"text": "Open the link\n\nGuide: https://example.com/guide"},
    }


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
        with patch("main.reply_to_comment", new_callable=AsyncMock) as reply, patch(
            "main.send_direct_message", new_callable=AsyncMock, return_value="commenter-2"
        ) as message:
            assert client.post("/api/webhooks/instagram", content=body, headers=headers).status_code == 200
            assert client.post("/api/webhooks/instagram", content=body, headers=headers).status_code == 200
            reply.assert_awaited_once_with("comment-1", "Thanks!", "private-token")
            assert message.await_count == 1
            assert message.await_args.kwargs["comment_id"] == "comment-1"
        with connection() as db:
            assert db.execute("SELECT count(*) FROM processed_comments").fetchone()[0] == 1

        assert client.delete(
            "/api/automations/post-1", headers={"X-Requested-With": "XMLHttpRequest"}
        ).status_code == 200
        assert client.get("/api/automations").json()["data"] == []
        with patch("main.get_posts", new_callable=AsyncMock, return_value=[sample_post]):
            assert client.put(
                f"/api/automation-drafts/{draft.json()['id']}",
                json={**draft_input, "comment_replies_enabled": True, "comment_replies": ["Sent to DM"]},
                headers={"X-Requested-With": "XMLHttpRequest"},
            ).status_code == 200
        draft_only_event = {"entry": [{"id": "ig-1", "changes": [{"field": "comments", "value": {
            "id": "comment-2", "media": {"id": "post-1"}, "text": "Hello again"
        }}]}]}
        draft_only_body = json.dumps(draft_only_event).encode()
        draft_only_signature = "sha256=" + hmac.new(b"test-secret", draft_only_body, hashlib.sha256).hexdigest()
        with patch("main.reply_to_comment", new_callable=AsyncMock) as reply, patch(
            "main.send_direct_message", new_callable=AsyncMock, return_value="commenter-2"
        ) as message:
            assert client.post(
                "/api/webhooks/instagram", content=draft_only_body,
                headers={"X-Hub-Signature-256": draft_only_signature},
            ).status_code == 200
            assert client.post(
                "/api/webhooks/instagram", content=draft_only_body,
                headers={"X-Hub-Signature-256": draft_only_signature},
            ).status_code == 200
            reply.assert_awaited_once_with("comment-2", "Sent to DM", "private-token")
            assert message.await_count == 1
            assert message.await_args.kwargs["comment_id"] == "comment-2"
            assert message.await_args.args[1] == ""
        with connection() as db:
            assert db.execute(
                "SELECT sender_id FROM pending_subscription_checks WHERE draft_id = ?",
                (draft.json()["id"],),
            ).fetchone()[0] == "commenter-2"
        typed_confirmation = {"entry": [{"id": "ig-1", "messaging": [{
            "sender": {"id": "commenter-2"},
            "message": {"mid": "typed-confirmation-1", "text": "Done"},
        }]}]}
        typed_body = json.dumps(typed_confirmation).encode()
        typed_signature = "sha256=" + hmac.new(b"test-secret", typed_body, hashlib.sha256).hexdigest()
        with patch("main.check_subscription", new_callable=AsyncMock, return_value=True), patch(
            "main.send_direct_message", new_callable=AsyncMock
        ) as message:
            assert client.post(
                "/api/webhooks/instagram", content=typed_body,
                headers={"X-Hub-Signature-256": typed_signature},
            ).status_code == 200
            message.assert_awaited_once()
            assert message.await_args.args[:3] == ("ig-1", "commenter-2", "Here is the guide")
        with connection() as db:
            assert db.execute(
                "SELECT count(*) FROM pending_subscription_checks WHERE draft_id = ?",
                (draft.json()["id"],),
            ).fetchone()[0] == 0
        private_reply_event = {"entry": [{"id": "ig-1", "changes": [{"field": "comments", "value": {
            "id": "comment-3", "media": {"id": "post-1"}, "text": "Hello again",
            "from": {"id": "commenter-1"},
        }}]}]}
        private_reply_body = json.dumps(private_reply_event).encode()
        private_reply_signature = "sha256=" + hmac.new(
            b"test-secret", private_reply_body, hashlib.sha256
        ).hexdigest()
        with patch("main.check_subscription", new_callable=AsyncMock, return_value=True), patch(
            "main.reply_to_comment", new_callable=AsyncMock
        ) as reply, patch(
            "main.send_direct_message", new_callable=AsyncMock
        ) as message:
            assert client.post(
                "/api/webhooks/instagram", content=private_reply_body,
                headers={"X-Hub-Signature-256": private_reply_signature},
            ).status_code == 200
            assert client.post(
                "/api/webhooks/instagram", content=private_reply_body,
                headers={"X-Hub-Signature-256": private_reply_signature},
            ).status_code == 200
            reply.assert_awaited_once_with("comment-3", "Sent to DM", "private-token")
            assert message.await_count == 1
            assert message.await_args.kwargs["comment_id"] == "comment-3"
            assert message.await_args.args[:3] == ("ig-1", "commenter-1", "Here is the guide")
        failed_dm_event = {"entry": [{"id": "ig-1", "changes": [{"field": "comments", "value": {
            "id": "comment-4", "media": {"id": "post-1"}, "text": "Hello again",
            "from": {"id": "commenter-1"},
        }}]}]}
        failed_dm_body = json.dumps(failed_dm_event).encode()
        failed_dm_signature = "sha256=" + hmac.new(b"test-secret", failed_dm_body, hashlib.sha256).hexdigest()
        with patch("main.check_subscription", new_callable=AsyncMock, return_value=True), patch(
            "main.reply_to_comment", new_callable=AsyncMock
        ) as reply, patch(
            "main.send_direct_message", new_callable=AsyncMock,
            side_effect=[InstagramError("DM failed"), None]
        ) as message:
            for _ in range(2):
                assert client.post(
                    "/api/webhooks/instagram", content=failed_dm_body,
                    headers={"X-Hub-Signature-256": failed_dm_signature},
                ).status_code == 200
            reply.assert_awaited_once_with("comment-4", "Sent to DM", "private-token")
            assert message.await_count == 2
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
