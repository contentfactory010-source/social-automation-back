"""Social Automation API. Run with: uvicorn main:app --reload"""

import hashlib
import hmac
import base64
import binascii
import json
import logging
import os
import secrets
try:
    import sqlite3
except ImportError:  # Cloudflare Python Workers do not provide sqlite3.
    class _SQLiteCompat:
        Row = dict
        IntegrityError = Exception

    sqlite3 = _SQLiteCompat()
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import urlencode

try:
    from dotenv import load_dotenv
except ImportError:  # Worker variables are provided by Cloudflare bindings.
    def load_dotenv() -> None:
        return None
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from db import connection, init_db
from instagram import (
    InstagramError,
    authorization_url,
    exchange_code,
    get_posts,
    get_profile,
    reply_to_comment,
    send_direct_message,
    setting,
    subscribe_to_webhooks,
)


load_dotenv()
logger = logging.getLogger("uvicorn.error")
SESSION_COOKIE = "sa_session"
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax").lower()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Social Automation API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Requested-With"],
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redirect_front(error: Optional[str] = None) -> RedirectResponse:
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(f"{FRONTEND_URL}/{query}", status_code=303)


def set_session_cookie(response: RedirectResponse, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=60 * 60 * 24 * 60,
    )


def get_session(request: Request) -> sqlite3.Row:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise HTTPException(401, "Connect Instagram first")
    with connection() as db:
        row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row["access_token"]:
        raise HTTPException(401, "Connect Instagram first")
    return row


def require_browser_request(request: Request, x_requested_with: Optional[str] = Header(None)) -> None:
    if x_requested_with != "XMLHttpRequest":
        raise HTTPException(403, "Missing request header")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != FRONTEND_URL:
        raise HTTPException(403, "Invalid origin")


class AutomationInput(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=2000)


class AutomationEnabledInput(BaseModel):
    enabled: bool


class AutomationDraftInput(BaseModel):
    triggers: list[Literal["comment", "direct"]] = Field(min_length=1, max_length=2)
    check_subscription: bool
    comment_mode: Literal["any", "keywords"]
    keywords: list[str] = Field(max_length=30)
    exact_match: bool
    post_scope: Literal["all", "selected"]
    post_ids: list[str] = Field(max_length=500)
    welcome_message: str = Field(min_length=1, max_length=500)
    welcome_button: str = Field(min_length=1, max_length=50)
    welcome_image: Optional[str] = None
    no_subscription_message: str = Field(max_length=500)
    no_subscription_button: str = Field(max_length=50)
    after_subscription_message: str = Field(max_length=500)


def draft_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "config": json.loads(row["config_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status": "active" if row["enabled"] else "inactive",
        "enabled": bool(row["enabled"]),
    }


def validate_draft(data: AutomationDraftInput) -> None:
    if len(set(data.triggers)) != len(data.triggers):
        raise HTTPException(422, "Choose each trigger once")
    if "comment" in data.triggers and not data.post_ids:
        raise HTTPException(422, "Select at least one post")
    if "comment" in data.triggers and data.comment_mode == "keywords" and (not data.keywords or any(not word.strip() or len(word) > 200 for word in data.keywords)):
        raise HTTPException(422, "Enter at least one keyword, up to 200 characters each")
    if data.check_subscription and not all([
        data.no_subscription_message.strip(), data.no_subscription_button.strip(), data.after_subscription_message.strip()
    ]):
        raise HTTPException(422, "Complete subscription messages")
    if not data.welcome_message.strip() or not data.welcome_button.strip():
        raise HTTPException(422, "Complete the welcome message")
    if data.welcome_image:
        try:
            header, encoded = data.welcome_image.split(",", 1)
            if header not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"}:
                raise ValueError("Unsupported image type")
            if len(encoded) > 1400000:
                raise ValueError("Image exceeds 1 MB")
            if len(base64.b64decode(encoded, validate=True)) > 1024 * 1024:
                raise ValueError("Image exceeds 1 MB")
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(422, "Choose a PNG, JPEG, or WebP image under 1 MB") from exc


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/auth/instagram/start")
def instagram_start(request: Request) -> RedirectResponse:
    try:
        state = secrets.token_urlsafe(32)
        session_id = request.cookies.get(SESSION_COOKIE) or secrets.token_urlsafe(32)
        with connection() as db:
            db.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", (session_id,))
            db.execute(
                "INSERT INTO oauth_states (state, session_id, created_at) VALUES (?, ?, ?)",
                (state, session_id, now()),
            )
        response = RedirectResponse(authorization_url(state), status_code=302)
        set_session_cookie(response, session_id)
        return response
    except InstagramError as exc:
        return redirect_front(str(exc))


@app.get("/api/auth/instagram/callback")
async def instagram_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    if error:
        return redirect_front("Instagram authorization was cancelled")
    session_id = request.cookies.get(SESSION_COOKIE)
    if not code or not state or not session_id:
        return redirect_front("Instagram authorization response is incomplete")

    with connection() as db:
        saved = db.execute("SELECT * FROM oauth_states WHERE state = ? AND session_id = ?", (state, session_id)).fetchone()
        db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    if not saved or datetime.fromisoformat(saved["created_at"]) < datetime.now(timezone.utc) - timedelta(minutes=10):
        return redirect_front("Instagram authorization expired. Try again")

    try:
        token = await exchange_code(code)
        profile = await get_profile(token["access_token"])
    except InstagramError as exc:
        logger.warning("Instagram connection failed: %s", exc)
        return redirect_front(str(exc))

    # Webhooks identify the professional account with `/me.user_id`.
    # `/me.id` is app-scoped and does not match the webhook entry ID.
    instagram_user_id = str(profile.get("user_id") or token["ig_user_id"])
    logger.info(
        "Instagram profile resolved: app_scoped_id=%s webhook_user_id=%s username=%s",
        profile.get("id"),
        instagram_user_id,
        profile.get("username", ""),
    )
    with connection() as db:
        previous = db.execute("SELECT ig_user_id, username FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if (
            previous
            and previous["ig_user_id"]
            and previous["ig_user_id"] != instagram_user_id
            and previous["username"] != profile.get("username", "")
        ):
            db.execute("DELETE FROM automations WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM automation_drafts WHERE session_id = ?", (session_id,))
        db.execute(
            """UPDATE sessions SET ig_user_id = ?, username = ?, access_token = ?,
               token_expires_at = ?, connected_at = ? WHERE id = ?""",
            (instagram_user_id, profile.get("username", ""), token["access_token"],
             token["token_expires_at"], now(), session_id),
        )
    try:
        await subscribe_to_webhooks(instagram_user_id, token["access_token"])
        logger.info("Instagram webhooks subscribed for account %s", instagram_user_id)
    except InstagramError as exc:
        # A connected account is still useful if webhook subscription needs
        # to be completed in Meta Developer or the account lacks a permission.
        logger.warning("Instagram webhook subscription failed: %s", exc)
    response = redirect_front()
    set_session_cookie(response, session_id)
    return response


@app.get("/api/connection")
def get_connection(request: Request) -> dict:
    try:
        session = get_session(request)
    except HTTPException:
        return {"connected": False}
    return {
        "connected": True,
        "instagramUserId": session["ig_user_id"],
        "username": session["username"],
        "connectedAt": session["connected_at"],
    }


@app.delete("/api/connection", dependencies=[Depends(require_browser_request)])
def disconnect(request: Request, session: sqlite3.Row = Depends(get_session)) -> dict:
    with connection() as db:
        db.execute("DELETE FROM automations WHERE session_id = ?", (session["id"],))
        db.execute("DELETE FROM automation_drafts WHERE session_id = ?", (session["id"],))
        db.execute(
            """UPDATE sessions SET ig_user_id = NULL, username = NULL, access_token = NULL,
               token_expires_at = NULL, connected_at = NULL WHERE id = ?""",
            (session["id"],),
        )
    return {"connected": False}


@app.get("/api/posts")
async def posts(session: sqlite3.Row = Depends(get_session)) -> dict:
    try:
        return {"data": await get_posts(session["access_token"])}
    except InstagramError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/automations")
def automations(session: sqlite3.Row = Depends(get_session)) -> dict:
    with connection() as db:
        rows = db.execute(
            "SELECT id, post_id, keyword, text, enabled, updated_at FROM automations WHERE session_id = ? ORDER BY updated_at DESC",
            (session["id"],),
        ).fetchall()
    return {"data": [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]}


@app.get("/api/automation-drafts")
def automation_drafts(session: sqlite3.Row = Depends(get_session)) -> dict:
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM automation_drafts WHERE session_id = ? ORDER BY updated_at DESC",
            (session["id"],),
        ).fetchall()
    return {"data": [draft_row(row) for row in rows]}


@app.post("/api/automation-drafts", status_code=201, dependencies=[Depends(require_browser_request)])
async def create_automation_draft(data: AutomationDraftInput, session: sqlite3.Row = Depends(get_session)) -> dict:
    validate_draft(data)
    if data.post_ids:
        try:
            owned_posts = await get_posts(session["access_token"])
        except InstagramError as exc:
            raise HTTPException(502, str(exc)) from exc
        if not set(data.post_ids).issubset({post["id"] for post in owned_posts}):
            raise HTTPException(404, "Instagram post not found")

    draft_id = secrets.token_urlsafe(16)
    timestamp = now()
    with connection() as db:
        db.execute(
            "INSERT INTO automation_drafts (id, session_id, config_json, enabled, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
            (draft_id, session["id"], data.model_dump_json(), timestamp, timestamp),
        )
        row = db.execute("SELECT * FROM automation_drafts WHERE id = ?", (draft_id,)).fetchone()
    return draft_row(row)


@app.put("/api/automation-drafts/{draft_id}", dependencies=[Depends(require_browser_request)])
async def update_automation_draft(
    draft_id: str,
    data: AutomationDraftInput,
    session: sqlite3.Row = Depends(get_session),
) -> dict:
    validate_draft(data)
    if data.post_ids:
        try:
            owned_posts = await get_posts(session["access_token"])
        except InstagramError as exc:
            raise HTTPException(502, str(exc)) from exc
        if not set(data.post_ids).issubset({post["id"] for post in owned_posts}):
            raise HTTPException(404, "Instagram post not found")

    timestamp = now()
    with connection() as db:
        existing = db.execute(
            "SELECT id FROM automation_drafts WHERE id = ? AND session_id = ?",
            (draft_id, session["id"]),
        ).fetchone()
        if not existing:
            raise HTTPException(404, "Automation not found")
        db.execute(
            "UPDATE automation_drafts SET config_json = ?, updated_at = ? WHERE id = ? AND session_id = ?",
            (data.model_dump_json(), timestamp, draft_id, session["id"]),
        )
        row = db.execute("SELECT * FROM automation_drafts WHERE id = ?", (draft_id,)).fetchone()
    return draft_row(row)


@app.put("/api/automations/{post_id}", dependencies=[Depends(require_browser_request)])
async def save_automation(post_id: str, data: AutomationInput, session: sqlite3.Row = Depends(get_session)) -> dict:
    keyword, text = data.keyword.strip(), data.text.strip()
    if not keyword or not text:
        raise HTTPException(422, "Keyword and reply text are required")
    try:
        owned_posts = await get_posts(session["access_token"])
    except InstagramError as exc:
        raise HTTPException(502, str(exc)) from exc
    if post_id not in {post["id"] for post in owned_posts}:
        raise HTTPException(404, "Instagram post not found")

    with connection() as db:
        db.execute(
            """INSERT INTO automations (id, session_id, post_id, keyword, text, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id, post_id) DO UPDATE SET
               keyword = excluded.keyword, text = excluded.text, updated_at = excluded.updated_at""",
            (secrets.token_urlsafe(16), session["id"], post_id, keyword, text, now()),
        )
        row = db.execute(
            "SELECT id, post_id, keyword, text, enabled, updated_at FROM automations WHERE session_id = ? AND post_id = ?",
            (session["id"], post_id),
        ).fetchone()
    return {**dict(row), "enabled": bool(row["enabled"])}


@app.patch("/api/automations/{automation_id}/enabled", dependencies=[Depends(require_browser_request)])
def set_automation_enabled(
    automation_id: str,
    data: AutomationEnabledInput,
    session: sqlite3.Row = Depends(get_session),
) -> dict:
    with connection() as db:
        row = db.execute(
            "SELECT id FROM automations WHERE id = ? AND session_id = ?",
            (automation_id, session["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Automation not found")
        db.execute(
            "UPDATE automations SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(data.enabled), now(), automation_id),
        )
        updated = db.execute(
            "SELECT id, post_id, keyword, text, enabled, updated_at FROM automations WHERE id = ?",
            (automation_id,),
        ).fetchone()
    return {**dict(updated), "enabled": bool(updated["enabled"])}


@app.patch("/api/automation-drafts/{draft_id}/enabled", dependencies=[Depends(require_browser_request)])
def set_draft_enabled(
    draft_id: str,
    data: AutomationEnabledInput,
    session: sqlite3.Row = Depends(get_session),
) -> dict:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM automation_drafts WHERE id = ? AND session_id = ?",
            (draft_id, session["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Automation not found")
        db.execute(
            "UPDATE automation_drafts SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(data.enabled), now(), draft_id),
        )
        updated = db.execute("SELECT * FROM automation_drafts WHERE id = ?", (draft_id,)).fetchone()
    return draft_row(updated)


@app.delete("/api/automations/{post_id}", dependencies=[Depends(require_browser_request)])
def delete_automation(post_id: str, session: sqlite3.Row = Depends(get_session)) -> dict:
    with connection() as db:
        db.execute("DELETE FROM automations WHERE session_id = ? AND post_id = ?", (session["id"], post_id))
    return {"deleted": True}


@app.get("/api/webhooks/instagram")
def verify_webhook(request: Request):
    params = request.query_params
    verify_token = setting("INSTAGRAM_WEBHOOK_VERIFY_TOKEN")
    if params.get("hub.mode") != "subscribe" or not secrets.compare_digest(params.get("hub.verify_token", ""), verify_token):
        raise HTTPException(403, "Invalid webhook verification")
    challenge = params.get("hub.challenge")
    if not challenge:
        raise HTTPException(400, "Missing challenge")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(challenge)


@app.post("/api/webhooks/instagram")
async def instagram_webhook(request: Request) -> dict:
    payload = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    digest = hmac.new(setting("INSTAGRAM_APP_SECRET").encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, f"sha256={digest}"):
        raise HTTPException(403, "Invalid webhook signature")
    try:
        event = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Invalid JSON") from exc

    logger.info(
        "Instagram webhook received: object=%s entries=%d",
        event.get("object"),
        len(event.get("entry", [])),
    )

    for entry in event.get("entry", []):
        ig_user_id = str(entry.get("id", ""))
        for change in entry.get("changes", []):
            if change.get("field") != "comments":
                continue
            value = change.get("value", {})
            comment_id = str(value.get("id", ""))
            post_id = str(value.get("media", {}).get("id", ""))
            comment_text = str(value.get("text", ""))
            commenter_id = str(value.get("from", {}).get("id", ""))
            logger.info(
                "Instagram comment event: account=%s comment_id=%s post_id=%s commenter=%s has_text=%s",
                ig_user_id,
                comment_id,
                post_id,
                commenter_id,
                bool(comment_text),
            )
            if not comment_id or not post_id or not comment_text or commenter_id == ig_user_id:
                continue
            with connection() as db:
                matches = db.execute(
                    """SELECT a.id, a.keyword, a.text, s.access_token FROM automations a
                       JOIN sessions s ON a.session_id = s.id
                       WHERE s.ig_user_id = ? AND a.post_id = ? AND a.enabled = 1 AND s.access_token IS NOT NULL""",
                    (ig_user_id, post_id),
                ).fetchall()
            for match in matches:
                if match["keyword"].casefold() not in comment_text.casefold():
                    continue
                try:
                    with connection() as db:
                        db.execute(
                            "INSERT INTO processed_comments (comment_id, automation_id, created_at) VALUES (?, ?, ?)",
                            (comment_id, match["id"], now()),
                        )
                except sqlite3.IntegrityError:
                    break
                try:
                    await reply_to_comment(comment_id, match["text"], match["access_token"])
                except InstagramError:
                    logger.exception("Could not reply to comment %s", comment_id)
                    with connection() as db:
                        db.execute("DELETE FROM processed_comments WHERE comment_id = ?", (comment_id,))
                    # The event was valid and already accepted. Returning a
                    # 2xx prevents Meta from retrying the same comment.
                    continue
                break

            with connection() as db:
                draft_rows = db.execute(
                    """SELECT d.*, s.access_token FROM automation_drafts d
                       JOIN sessions s ON d.session_id = s.id
                       WHERE s.ig_user_id = ? AND d.enabled = 1 AND s.access_token IS NOT NULL""",
                    (ig_user_id,),
                ).fetchall()
            for draft in draft_rows:
                config = json.loads(draft["config_json"])
                if "comment" not in config.get("triggers", []):
                    continue
                if config.get("post_scope") == "selected" and post_id not in config.get("post_ids", []):
                    continue
                keywords = [str(keyword).strip() for keyword in config.get("keywords", []) if str(keyword).strip()]
                if config.get("comment_mode") == "keywords":
                    if not any(
                        comment_text.casefold() == keyword.casefold()
                        if config.get("exact_match")
                        else keyword.casefold() in comment_text.casefold()
                        for keyword in keywords
                    ):
                        continue
                if not commenter_id:
                    continue
                try:
                    with connection() as db:
                        db.execute(
                            "INSERT INTO processed_draft_events (event_id, draft_id, created_at) VALUES (?, ?, ?)",
                            (comment_id, draft["id"], now()),
                        )
                except sqlite3.IntegrityError:
                    continue
                try:
                    await send_direct_message(
                        ig_user_id,
                        commenter_id,
                        config["welcome_message"],
                        draft["access_token"],
                    )
                except InstagramError:
                    logger.exception("Could not send automation message for comment %s", comment_id)
                    with connection() as db:
                        db.execute(
                            "DELETE FROM processed_draft_events WHERE event_id = ? AND draft_id = ?",
                            (comment_id, draft["id"]),
                        )

        for messaging in entry.get("messaging", []):
            message = messaging.get("message", {})
            message_id = str(message.get("mid", ""))
            message_text = str(message.get("text", "")).strip()
            sender_id = str(messaging.get("sender", {}).get("id", ""))
            logger.info(
                "Instagram message event: account=%s sender=%s message_id=%s has_text=%s echo=%s",
                ig_user_id,
                sender_id,
                message_id,
                bool(message_text),
                bool(message.get("is_echo")),
            )
            if not message_id or not message_text or not sender_id or sender_id == ig_user_id:
                continue

            # Direct-message rules use the reserved post id. The existing
            # simple automation table is retained for backwards compatibility
            # until the richer automation form is activated server-side.
            with connection() as db:
                matches = db.execute(
                    """SELECT a.id, a.keyword, a.text, s.access_token FROM automations a
                       JOIN sessions s ON a.session_id = s.id
                       WHERE s.ig_user_id = ? AND a.post_id = '__direct__' AND a.enabled = 1 AND s.access_token IS NOT NULL""",
                    (ig_user_id,),
                ).fetchall()
            logger.info("Legacy DM automation matches: %d", len(matches))
            for match in matches:
                if match["keyword"].casefold() not in message_text.casefold():
                    continue
                try:
                    with connection() as db:
                        db.execute(
                            "INSERT INTO processed_messages (message_id, automation_id, created_at) VALUES (?, ?, ?)",
                            (message_id, match["id"], now()),
                        )
                except sqlite3.IntegrityError:
                    break
                try:
                    await send_direct_message(ig_user_id, sender_id, match["text"], match["access_token"])
                    logger.info("Legacy Instagram DM reply sent: sender=%s message_id=%s", sender_id, message_id)
                except InstagramError:
                    logger.exception("Could not send direct message reply to %s", message_id)
                    with connection() as db:
                        db.execute("DELETE FROM processed_messages WHERE message_id = ?", (message_id,))
                break

            with connection() as db:
                draft_rows = db.execute(
                    """SELECT d.*, s.access_token FROM automation_drafts d
                       JOIN sessions s ON d.session_id = s.id
                       WHERE s.ig_user_id = ? AND d.enabled = 1 AND s.access_token IS NOT NULL""",
                    (ig_user_id,),
                ).fetchall()
            logger.info("Form DM automations available: %d", len(draft_rows))
            for draft in draft_rows:
                config = json.loads(draft["config_json"])
                if "direct" not in config.get("triggers", []):
                    continue
                try:
                    with connection() as db:
                        db.execute(
                            "INSERT INTO processed_draft_events (event_id, draft_id, created_at) VALUES (?, ?, ?)",
                            (message_id, draft["id"], now()),
                        )
                except sqlite3.IntegrityError:
                    continue
                try:
                    await send_direct_message(
                        ig_user_id,
                        sender_id,
                        config["welcome_message"],
                        draft["access_token"],
                    )
                    logger.info("Form Instagram DM reply sent: sender=%s message_id=%s", sender_id, message_id)
                except InstagramError:
                    logger.exception("Could not send automation message for DM %s", message_id)
                    with connection() as db:
                        db.execute(
                            "DELETE FROM processed_draft_events WHERE event_id = ? AND draft_id = ?",
                            (message_id, draft["id"]),
                        )
    return {"ok": True}
