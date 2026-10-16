import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from workers import fetch

from cloudflare_config import setting, value
from instagram import InstagramError


AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_URL = "https://graph.instagram.com"


def authorization_url(state: str) -> str:
    params = {
        "client_id": setting("INSTAGRAM_APP_ID"),
        "redirect_uri": setting("INSTAGRAM_REDIRECT_URI"),
        "response_type": "code",
        "scope": value(
            "INSTAGRAM_SCOPES",
            "instagram_business_basic,instagram_business_manage_comments,instagram_business_manage_messages",
        ),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def request(url: str, method: str = "GET", *, headers=None, body=None, params=None):
    if params:
        url = f"{url}?{urlencode(params)}"
    response = await fetch(url, method=method, headers=headers or {}, body=body)
    if response.status >= 400:
        message = await response.text()
        raise InstagramError(f"Instagram request failed ({response.status}): {message[:500]}")
    return response


async def exchange_code(code: str) -> dict:
    form = urlencode({
        "client_id": setting("INSTAGRAM_APP_ID"),
        "client_secret": setting("INSTAGRAM_APP_SECRET"),
        "grant_type": "authorization_code",
        "redirect_uri": setting("INSTAGRAM_REDIRECT_URI"),
        "code": code,
    })
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        short = await (await request(TOKEN_URL, "POST", headers=headers, body=form)).json()
        long = await (await request(f"{GRAPH_URL}/access_token", params={
            "grant_type": "ig_exchange_token",
            "client_secret": setting("INSTAGRAM_APP_SECRET"),
            "access_token": short["access_token"],
        })).json()
    except (InstagramError, KeyError, TypeError, ValueError) as exc:
        raise InstagramError("Instagram token exchange failed. Check app settings and redirect URI.") from exc
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=long.get("expires_in", 0))
    return {
        "ig_user_id": str(short["user_id"]),
        "access_token": long["access_token"],
        "token_expires_at": expires_at.isoformat(),
    }


async def get_profile(token: str) -> dict:
    response = await request(f"{GRAPH_URL}/me", params={
        "fields": "id,user_id,username",
        "access_token": token,
    })
    return await response.json()


async def get_user_profile(user_id: str, token: str) -> dict:
    response = await request(f"{GRAPH_URL}/{user_id}", params={
        "fields": "is_user_follow_business",
        "access_token": token,
    })
    return await response.json()


async def get_posts(token: str) -> list[dict]:
    posts = []
    url = f"{GRAPH_URL}/me/media"
    params = {
        "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp",
        "limit": "50",
        "access_token": token,
    }
    for _ in range(5):
        page = await (await request(url, params=params)).json()
        posts.extend(page.get("data", []))
        url = page.get("paging", {}).get("next")
        if not url:
            break
        params = None
    return posts


async def reply_to_comment(comment_id: str, message: str, token: str) -> None:
    form = urlencode({"message": message, "access_token": token})
    await request(
        f"{GRAPH_URL}/{comment_id}/replies",
        "POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=form,
    )


async def send_direct_message(
    ig_user_id: str,
    recipient_id: str,
    message: str,
    token: str,
    button_text: str = "",
    button_url: str = "",
    button_payload: str = "SOCIAL_AUTOMATION_REPLY",
) -> None:
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
    await request(
        f"{GRAPH_URL}/me/messages",
        "POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        body=json.dumps(payload),
    )


async def subscribe_to_webhooks(ig_user_id: str, token: str) -> None:
    form = urlencode({
        "subscribed_fields": value("INSTAGRAM_WEBHOOK_FIELDS", "comments,messages"),
        "access_token": token,
    })
    await request(
        f"{GRAPH_URL}/me/subscribed_apps",
        "POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=form,
    )
