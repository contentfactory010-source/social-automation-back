"""Calls for the Instagram Login flavor of Meta's API."""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

try:
    import httpx
except ImportError:  # Cloudflare uses the Worker Fetch API instead.
    httpx = None


AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_URL = "https://graph.instagram.com"


class InstagramError(Exception):
    pass


def setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise InstagramError(f"{name} is not configured")
    return value


def authorization_url(state: str) -> str:
    params = {
        "client_id": setting("INSTAGRAM_APP_ID"),
        "redirect_uri": setting("INSTAGRAM_REDIRECT_URI"),
        "response_type": "code",
        "scope": os.getenv(
            "INSTAGRAM_SCOPES",
            "instagram_business_basic,instagram_business_manage_comments,instagram_business_manage_messages",
        ),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(code: str, redirect_uri: str = "") -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.post(TOKEN_URL, data={
                "client_id": setting("INSTAGRAM_APP_ID"),
                "client_secret": setting("INSTAGRAM_APP_SECRET"),
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri or setting("INSTAGRAM_REDIRECT_URI"),
                "code": code,
            })
            response.raise_for_status()
            short = response.json()
            long_response = await client.get(f"{GRAPH_URL}/access_token", params={
                "grant_type": "ig_exchange_token",
                "client_secret": setting("INSTAGRAM_APP_SECRET"),
                "access_token": short["access_token"],
            })
            long_response.raise_for_status()
            long = long_response.json()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise InstagramError("Instagram token exchange failed. Check app settings and redirect URI.") from exc

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=long.get("expires_in", 0))
    return {
        "ig_user_id": str(short["user_id"]),
        "access_token": long["access_token"],
        "token_expires_at": expires_at.isoformat(),
    }


async def get_profile(token: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.get(f"{GRAPH_URL}/me", params={
                # `id` is app-scoped. `user_id` is the professional account
                # ID used as the webhook entry ID.
                "fields": "id,user_id,username",
                "access_token": token,
            })
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise InstagramError("Could not load Instagram profile") from exc


async def get_user_profile(user_id: str, token: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.get(f"{GRAPH_URL}/{user_id}", params={
                "fields": "is_user_follow_business",
                "access_token": token,
            })
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise InstagramError("Could not check Instagram subscription status") from exc


async def get_posts(token: str) -> list[dict]:
    posts = []
    url = f"{GRAPH_URL}/me/media"
    params = {
        "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp",
        "limit": "50",
        "access_token": token,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            for _ in range(5):
                response = await client.get(url, params=params)
                response.raise_for_status()
                page = response.json()
                posts.extend(page.get("data", []))
                url = page.get("paging", {}).get("next")
                if not url:
                    break
                params = None
        except (httpx.HTTPError, ValueError) as exc:
            raise InstagramError("Could not load Instagram posts. Reconnect the account if the token expired.") from exc
    return posts


async def reply_to_comment(comment_id: str, message: str, token: str) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.post(f"{GRAPH_URL}/{comment_id}/replies", data={
                "message": message,
                "access_token": token,
            })
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise InstagramError(
                f"Could not reply to Instagram comment ({exc.response.status_code}): {exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise InstagramError("Could not reply to Instagram comment") from exc


async def send_direct_message(
    ig_user_id: str,
    recipient_id: str,
    message: str,
    token: str,
    button_text: str = "",
    button_url: str = "",
    button_payload: str = "SOCIAL_AUTOMATION_REPLY",
) -> None:
    """Send a DM through Instagram's Send API.

    Instagram only allows this for a recipient who has already initiated the
    conversation with the connected professional account.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            if button_url.strip():
                payload = {
                    "recipient": {"id": recipient_id},
                    "message": {
                        "attachment": {
                            "type": "template",
                            "payload": {
                                "template_type": "button",
                                "text": message,
                                "buttons": [{
                                    "type": "web_url",
                                    "url": button_url.strip(),
                                    "title": (button_text.strip() or "Open link")[:20],
                                }],
                            },
                        },
                    },
                }
            else:
                payload = {
                    "recipient": {"id": recipient_id},
                    "message": {"text": message},
                }
            if button_text.strip() and not button_url.strip():
                payload["message"]["quick_replies"] = [{
                    "content_type": "text",
                    "title": button_text.strip()[:20],
                    "payload": button_payload[:1000],
                }]
            response = await client.post(
                f"{GRAPH_URL}/me/messages",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            return
        except httpx.HTTPStatusError as exc:
            raise InstagramError(
                f"Could not send Instagram direct message ({exc.response.status_code}): {exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise InstagramError("Could not send Instagram direct message") from exc


async def subscribe_to_webhooks(ig_user_id: str, token: str) -> None:
    """Subscribe the connected account to the webhook fields used by the app."""
    fields = os.getenv("INSTAGRAM_WEBHOOK_FIELDS", "comments,messages")
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.post(
                f"{GRAPH_URL}/me/subscribed_apps",
                data={"subscribed_fields": fields, "access_token": token},
            )
            response.raise_for_status()
            return
        except httpx.HTTPStatusError as exc:
            raise InstagramError(
                f"Could not subscribe the Instagram account to webhooks ({exc.response.status_code}): {exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise InstagramError("Could not subscribe the Instagram account to webhooks") from exc
