import asyncio
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from cf_bridge import verify_cf_access_token
from db import connection
from main import app


HEADERS = {"X-Requested-With": "XMLHttpRequest", "Origin": "http://localhost:3000"}
RETURN_TO = "http://localhost:3000/social-automation"


TOKEN_A = "first-browser-token"
TOKEN_A_OTHER_DEVICE = "second-browser-token"
TOKEN_B = "other-user-token"


def test_cf_token_is_checked_by_existing_me_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_API_BASE_URL", "https://cf.example/api")
    def respond(request):
        assert str(request.url) == "https://cf.example/api/auth/me"
        assert request.headers["authorization"] == "Bearer cf-token"
        return httpx.Response(200, json={"user_id": "user-a"})
    original_client = httpx.AsyncClient
    client = original_client(transport=httpx.MockTransport(respond))
    with patch("cf_bridge.httpx.AsyncClient", return_value=client):
        assert asyncio.run(verify_cf_access_token("cf-token")) == "user-a"

    denied = original_client(transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    with patch("cf_bridge.httpx.AsyncClient", return_value=denied):
        from fastapi import HTTPException
        try:
            asyncio.run(verify_cf_access_token("expired-token"))
            assert False, "Expired CF token must be rejected"
        except HTTPException as exc:
            assert exc.status_code == 401


def test_cf_login_is_bound_at_oauth_and_restored_on_another_device(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bridge.db"))
    monkeypatch.setenv("INSTAGRAM_APP_ID", "test-app")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "test-secret")
    monkeypatch.setenv("INSTAGRAM_REDIRECT_URI", "http://localhost:8000/api/auth/instagram/callback")
    async def verify(token):
        if token in (TOKEN_A, TOKEN_A_OTHER_DEVICE):
            return "user-a"
        if token == TOKEN_B:
            return "user-b"
        from fastapi import HTTPException
        raise HTTPException(401, "Invalid CF login")

    with patch("main.verify_cf_access_token", new_callable=AsyncMock, side_effect=verify), TestClient(app, base_url="https://testserver") as client:
        headers_a = {**HEADERS, "Authorization": f"Bearer {TOKEN_A}"}
        headers_a_other_device = {**HEADERS, "Authorization": f"Bearer {TOKEN_A_OTHER_DEVICE}"}
        headers_b = {**HEADERS, "Authorization": f"Bearer {TOKEN_B}"}
        assert client.post("/api/auth/instagram/prepare", json={"return_to": RETURN_TO}, headers=HEADERS).status_code == 401
        prepared = client.post("/api/auth/instagram/prepare", json={"return_to": RETURN_TO}, headers=headers_a)
        assert prepared.status_code == 200
        start = client.get(prepared.json()["url"], follow_redirects=False)
        assert start.status_code == 302
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        token = {"access_token": "instagram-token", "ig_user_id": "instagram-a", "token_expires_at": "2099-01-01T00:00:00+00:00"}
        with patch("main.exchange_code", new_callable=AsyncMock, return_value=token), patch(
            "main.get_profile", new_callable=AsyncMock, return_value={"user_id": "instagram-a", "username": "creator"}
        ), patch("main.subscribe_to_webhooks", new_callable=AsyncMock):
            callback = client.get("/api/auth/instagram/callback", params={"code": "valid", "state": state}, follow_redirects=False)
        ticket = parse_qs(urlparse(callback.headers["location"]).query)["sa_ticket"][0]
        wrong_user = client.post("/api/auth/instagram/claim", json={"ticket": ticket}, headers=headers_b)
        assert wrong_user.status_code == 403, wrong_user.json()
        claimed = client.post("/api/auth/instagram/claim", json={"ticket": ticket}, headers=headers_a)
        assert claimed.status_code == 200
        assert claimed.json()["cfUserId"] == "user-a"
        assert client.post("/api/auth/instagram/claim", json={"ticket": ticket}, headers=headers_a).status_code == 400
        restored = client.post("/api/client/session", headers=headers_a_other_device)
        assert restored.json()["sessionToken"] == claimed.json()["sessionToken"]
        assert client.post("/api/client/session", headers=headers_b).json() == {"connected": False}
        assert client.post("/api/client/session", headers=HEADERS).status_code == 401
        with connection() as db:
            assert db.execute("SELECT ig_user_id FROM cf_user_connections WHERE cf_user_id = ?", ("user-a",)).fetchone()[0] == "instagram-a"

        another = client.post("/api/auth/instagram/prepare", json={"return_to": RETURN_TO}, headers=headers_b)
        next_start = client.get(another.json()["url"], follow_redirects=False)
        next_state = parse_qs(urlparse(next_start.headers["location"]).query)["state"][0]
        with patch("main.exchange_code", new_callable=AsyncMock, return_value=token), patch(
            "main.get_profile", new_callable=AsyncMock, return_value={"user_id": "instagram-a", "username": "creator"}
        ):
            refused = client.get("/api/auth/instagram/callback", params={"code": "valid", "state": next_state}, follow_redirects=False)
        assert "error=" in refused.headers["location"]
        assert client.post("/api/client/session", headers=headers_b).json() == {"connected": False}
        assert client.post("/api/client/session", headers=headers_a_other_device).json()["sessionToken"] == claimed.json()["sessionToken"]
