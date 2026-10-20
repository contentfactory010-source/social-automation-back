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
from urllib.parse import urlencode, urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # Worker variables are provided by Cloudflare bindings.
    def load_dotenv() -> None:
        return None
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
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
    get_user_profile,
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
CORS_ALLOWED_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:3007",
    "https://app.neuro-c.ai",
    "https://content-factory-front.web.app",
    "https://content-factory-front.firebaseapp.com",
    "https://content-factory-stage.web.app",
    "https://content-factory-stage.firebaseapp.com",
    "https://saas-dev-eb027.web.app",
)


def browser_origins() -> set[str]:
    return {FRONTEND_URL, *(origin.rstrip("/") for origin in CORS_ALLOWED_ORIGINS)}

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Social Automation API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(browser_origins()),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Requested-With", "Authorization"],
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redirect_front(error: Optional[str] = None, return_to: Optional[str] = None) -> RedirectResponse:
    query = f"?{urlencode({'error': error})}" if error else ""
    return RedirectResponse(f"{return_to or FRONTEND_URL + '/'}{query}", status_code=303)


def valid_return_to(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in ("http", "https")
        and f"{parsed.scheme}://{parsed.netloc}" in browser_origins()
        and parsed.path == "/social-automation"
        and not parsed.username and not parsed.password
        and not parsed.query and not parsed.fragment
    )


def callback_return_to(state: str) -> Optional[str]:
    if "." not in state:
        return None
    try:
        value = state.split(".", 1)[1]
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    return decoded if valid_return_to(decoded) else None


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=60 * 60 * 24 * 60,
    )


def get_session(request: Request) -> sqlite3.Row:
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, session_id = authorization.partition(" ")
        if scheme.lower() != "bearer" or not session_id.strip():
            raise HTTPException(401, "Invalid session token")
        session_id = session_id.strip()
    else:
        session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise HTTPException(401, "Connect Instagram first")
    with connection() as db:
        row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row["access_token"]:
        raise HTTPException(401, "Connect Instagram first")
    return row


def connect_account(db, ig_user_id: str, username: str, access_token: str,
                    token_expires_at: Optional[str], candidate_session_id: Optional[str] = None) -> str:
    candidate = db.execute(
        "SELECT ig_user_id FROM sessions WHERE id = ?", (candidate_session_id,)
    ).fetchone() if candidate_session_id else None

    duplicate = db.execute(
        """SELECT post_id FROM automations
           WHERE session_id IN (SELECT id FROM sessions WHERE ig_user_id = ?)
           GROUP BY post_id HAVING COUNT(*) > 1 LIMIT 1""",
        (ig_user_id,),
    ).fetchone()
    if duplicate:
        raise HTTPException(409, "Duplicate saved rules for an Instagram post need to be resolved")

    # Rotate the session so an old cookie or bearer ID cannot become valid again.
    session_id = candidate_session_id if candidate and not candidate["ig_user_id"] else secrets.token_urlsafe(32)
    db.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", (session_id,))

    if candidate and candidate["ig_user_id"] and candidate["ig_user_id"] != ig_user_id:
        previous_account = candidate["ig_user_id"]
        db.execute("DELETE FROM pending_subscription_checks WHERE ig_user_id = ?", (previous_account,))
        db.execute(
            "UPDATE sessions SET access_token = NULL, token_expires_at = NULL WHERE ig_user_id = ?",
            (previous_account,),
        )

    sources = db.execute("SELECT id FROM sessions WHERE ig_user_id = ? AND id != ?", (ig_user_id, session_id)).fetchall()
    for source in sources:
        old_id = source["id"]
        db.execute("UPDATE automations SET session_id = ? WHERE session_id = ?", (session_id, old_id))
        db.execute("UPDATE automation_drafts SET session_id = ? WHERE session_id = ?", (session_id, old_id))
        db.execute("UPDATE pending_subscription_checks SET session_id = ? WHERE session_id = ?", (session_id, old_id))
        db.execute("DELETE FROM sessions WHERE id = ?", (old_id,))

    db.execute(
        """UPDATE sessions SET ig_user_id = ?, username = ?, access_token = ?,
           token_expires_at = ?, connected_at = ? WHERE id = ?""",
        (ig_user_id, username, access_token, token_expires_at, now(), session_id),
    )
    return session_id


def require_browser_request(request: Request, x_requested_with: Optional[str] = Header(None)) -> None:
    if x_requested_with != "XMLHttpRequest":
        raise HTTPException(403, "Missing request header")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in browser_origins():
        raise HTTPException(403, "Invalid origin")


class InstagramConnectionInput(BaseModel):
    access_token: str = Field(min_length=1, max_length=4096, repr=False)


class InstagramCodeInput(BaseModel):
    code: str = Field(min_length=1, max_length=4096, repr=False)
    redirect_uri: str = Field(min_length=1, max_length=2048)


class InstagramHandoffInput(BaseModel):
    ticket: str = Field(min_length=1, max_length=256)


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
    welcome_button: str = Field(max_length=50)
    welcome_image: Optional[str] = None
    no_subscription_message: str = Field(max_length=500)
    no_subscription_button: str = Field(max_length=50)
    after_subscription_message: str = Field(max_length=500)
    after_subscription_button: str = Field(default="", max_length=50)
    after_subscription_url: str = Field(default="", max_length=2000)
    comment_replies_enabled: bool = False
    comment_replies: list[str] = Field(default_factory=list, max_length=30)
    link_reminder_enabled: bool = False
    link_reminder_delay: int = Field(default=10, ge=1, le=1440)
    additional_message_enabled: bool = False
    additional_message_delay: int = Field(default=10, ge=1, le=1440)


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
        data.no_subscription_message.strip(), data.after_subscription_message.strip()
    ]):
        raise HTTPException(422, "Complete subscription messages")
    if not data.welcome_message.strip():
        raise HTTPException(422, "Complete the welcome message")
    if data.after_subscription_url.strip() and not data.after_subscription_url.strip().startswith(("http://", "https://")):
        raise HTTPException(422, "The subscription link must start with http:// or https://")
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
    if any(not message.strip() or len(message) > 500 for message in data.comment_replies):
        raise HTTPException(422, "Comment replies must contain text up to 500 characters")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/auth/instagram/start")
def instagram_start(request: Request, return_to: str = "") -> RedirectResponse:
    if return_to and (len(return_to) > 2048 or not valid_return_to(return_to)):
        raise HTTPException(400, "Invalid return URL")
    try:
        state = secrets.token_urlsafe(32)
        if return_to:
            state += "." + base64.urlsafe_b64encode(return_to.encode()).decode().rstrip("=")
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
    return_to = callback_return_to(state)
    session_id = request.cookies.get(SESSION_COOKIE)
    if not state or not session_id:
        return redirect_front("Instagram authorization response is incomplete", return_to)

    with connection() as db:
        saved = db.execute("SELECT * FROM oauth_states WHERE state = ? AND session_id = ?", (state, session_id)).fetchone()
        db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    if not saved or datetime.fromisoformat(saved["created_at"]) < datetime.now(timezone.utc) - timedelta(minutes=10):
        return redirect_front("Instagram authorization expired. Try again", return_to)

    if error:
        return redirect_front("Instagram authorization was cancelled", return_to)
    if not code:
        return redirect_front("Instagram authorization response is incomplete", return_to)

    try:
        token = await exchange_code(code)
        profile = await get_profile(token["access_token"])
    except InstagramError as exc:
        logger.warning("Instagram connection failed: %s", exc)
        return redirect_front(str(exc), return_to)

    # Webhooks identify the professional account with `/me.user_id`.
    # `/me.id` is app-scoped and does not match the webhook entry ID.
    instagram_user_id = str(profile.get("user_id") or token["ig_user_id"])
    logger.info(
        "Instagram profile resolved: app_scoped_id=%s webhook_user_id=%s username=%s",
        profile.get("id"),
        instagram_user_id,
        profile.get("username", ""),
    )
    try:
        with connection() as db:
            session_id = connect_account(
                db, instagram_user_id, profile.get("username", ""), token["access_token"],
                token["token_expires_at"], session_id,
            )
    except HTTPException as exc:
        return redirect_front(str(exc.detail), return_to)
    try:
        await subscribe_to_webhooks(instagram_user_id, token["access_token"])
        logger.info("Instagram webhooks subscribed for account %s", instagram_user_id)
    except InstagramError as exc:
        # A connected account is still useful if webhook subscription needs
        # to be completed in Meta Developer or the account lacks a permission.
        logger.warning("Instagram webhook subscription failed: %s", exc)
    if return_to:
        ticket = "handoff." + secrets.token_urlsafe(32)
        with connection() as db:
            db.execute(
                "INSERT INTO oauth_states (state, session_id, created_at) VALUES (?, ?, ?)",
                (ticket, session_id, now()),
            )
        response = RedirectResponse(f"{return_to}?{urlencode({'sa_ticket': ticket})}", status_code=303)
        response.headers["Cache-Control"] = "no-store"
    else:
        response = redirect_front()
    set_session_cookie(response, session_id)
    return response


@app.post("/api/auth/instagram/claim", dependencies=[Depends(require_browser_request)])
def claim_instagram_handoff(data: InstagramHandoffInput, response: Response) -> dict:
    if not data.ticket.startswith("handoff."):
        raise HTTPException(400, "Invalid handoff ticket")
    with connection() as db:
        row = db.execute("SELECT session_id, created_at FROM oauth_states WHERE state = ?", (data.ticket,)).fetchone()
        deleted = db.execute("DELETE FROM oauth_states WHERE state = ?", (data.ticket,))
        session = db.execute("SELECT * FROM sessions WHERE id = ?", (row["session_id"],)).fetchone() if row and deleted.rowcount == 1 else None
    if not row or deleted.rowcount != 1 or not session or not session["access_token"] or datetime.fromisoformat(row["created_at"]) < datetime.now(timezone.utc) - timedelta(minutes=5):
        raise HTTPException(400, "Invalid or expired handoff ticket")
    response.headers["Cache-Control"] = "no-store"
    return {
        "connected": True,
        "instagramUserId": session["ig_user_id"],
        "username": session["username"],
        "connectedAt": session["connected_at"],
        "sessionToken": session["id"],
    }


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


@app.post("/api/auth/instagram/exchange", dependencies=[Depends(require_browser_request)])
async def exchange_content_factory_code(data: InstagramCodeInput, response: Response) -> dict:
    redirect = urlparse(data.redirect_uri)
    origin = f"{redirect.scheme}://{redirect.netloc}"
    if (
        origin not in browser_origins()
        or redirect.path.rstrip("/") != "/social-automation"
        or redirect.query
        or redirect.fragment
    ):
        raise HTTPException(403, "Invalid Content Factory redirect URI")
    try:
        token = await exchange_code(data.code, data.redirect_uri)
    except InstagramError as exc:
        raise HTTPException(400, str(exc)) from exc
    response.headers["Cache-Control"] = "no-store"
    return {
        "accessToken": token["access_token"],
        "instagramUserId": str(token["ig_user_id"]),
        "connectedAt": now(),
    }


@app.post("/api/connection", dependencies=[Depends(require_browser_request)])
async def connect_existing_instagram(data: InstagramConnectionInput, response: Response) -> dict:
    try:
        profile = await get_profile(data.access_token)
    except InstagramError as exc:
        raise HTTPException(401, "Could not verify Instagram credentials. Reconnect Instagram.") from exc

    instagram_user_id = str(profile.get("user_id") or profile.get("id") or "")
    if not instagram_user_id:
        raise HTTPException(401, "Instagram profile does not include an account ID")

    with connection() as db:
        session_id = connect_account(db, instagram_user_id, profile.get("username", ""), data.access_token, None)

    try:
        await subscribe_to_webhooks(instagram_user_id, data.access_token)
    except InstagramError as exc:
        logger.warning("Instagram webhook subscription failed: %s", exc)

    set_session_cookie(response, session_id)
    response.headers["Cache-Control"] = "no-store"
    return {
        "connected": True,
        "instagramUserId": instagram_user_id,
        "username": profile.get("username", ""),
        "connectedAt": now(),
        "sessionToken": session_id,
    }


@app.delete("/api/connection", dependencies=[Depends(require_browser_request)])
def disconnect(request: Request, session: sqlite3.Row = Depends(get_session)) -> dict:
    with connection() as db:
        db.execute("DELETE FROM pending_subscription_checks WHERE ig_user_id = ?", (session["ig_user_id"],))
        db.execute(
            "UPDATE sessions SET access_token = NULL, token_expires_at = NULL WHERE ig_user_id = ?",
            (session["ig_user_id"],),
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
async def create_automation_draft(
    data: AutomationDraftInput,
    session: sqlite3.Row = Depends(get_session),
    replace_automation_id: str = "",
) -> dict:
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
        enabled = 1
        if replace_automation_id:
            previous = db.execute(
                "SELECT enabled FROM automations WHERE id = ? AND session_id = ?",
                (replace_automation_id, session["id"]),
            ).fetchone()
            if not previous:
                raise HTTPException(404, "Automation not found")
            enabled = previous["enabled"]
        db.execute(
            "INSERT INTO automation_drafts (id, session_id, config_json, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (draft_id, session["id"], data.model_dump_json(), enabled, timestamp, timestamp),
        )
        if replace_automation_id:
            db.execute("DELETE FROM automations WHERE id = ? AND session_id = ?", (replace_automation_id, session["id"]))
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


async def check_subscription(user_id: str, token: str) -> bool:
    try:
        profile = await get_user_profile(user_id, token)
        return bool(profile.get("is_user_follow_business", False))
    except InstagramError:
        logger.exception("Could not check Instagram subscription for user %s", user_id)
        return False


def draft_value(config: dict, snake_name: str, camel_name: str, default=None):
    return config.get(snake_name, config.get(camel_name, default))


def keyword_matches(comment_text: str, keywords: list[str], exact_match: bool) -> bool:
    normalized_text = comment_text.strip().casefold()
    return any(
        normalized_text == keyword.casefold()
        if exact_match
        else keyword.casefold() in normalized_text
        for keyword in keywords
    )


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
        with connection() as db:
            canonical_session = db.execute(
                "SELECT id FROM sessions WHERE ig_user_id = ? ORDER BY connected_at DESC LIMIT 1",
                (ig_user_id,),
            ).fetchone()
        canonical_session_id = canonical_session["id"] if canonical_session else ""
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
                       WHERE s.ig_user_id = ? AND s.id = ? AND a.post_id = ?
                         AND a.enabled = 1 AND s.access_token IS NOT NULL""",
                    (ig_user_id, canonical_session_id, post_id),
                ).fetchall()
            logger.info("Legacy comment automations matches: %d", len(matches))
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
                       WHERE s.ig_user_id = ? AND s.id = ? AND d.enabled = 1
                         AND s.access_token IS NOT NULL""",
                    (ig_user_id, canonical_session_id),
                ).fetchall()
            for draft in draft_rows:
                config = json.loads(draft["config_json"])
                triggers = draft_value(config, "triggers", "triggers", [])
                if "comment" not in triggers:
                    continue
                post_scope = draft_value(config, "post_scope", "postScope", "selected")
                post_ids = draft_value(config, "post_ids", "postIds", [])
                if post_scope == "selected" and post_id not in post_ids:
                    continue
                keywords = [str(keyword).strip() for keyword in draft_value(config, "keywords", "keywords", []) if str(keyword).strip()]
                comment_mode = draft_value(config, "comment_mode", "commentMode", "keywords")
                exact_match = bool(draft_value(config, "exact_match", "exactMatch", False))
                if comment_mode == "keywords" and not keyword_matches(comment_text, keywords, exact_match):
                    continue
                if comment_mode not in {"any", "keywords"}:
                    continue
                if not commenter_id:
                    continue
                if draft_value(config, "check_subscription", "checkSubscription", False):
                    subscribed = await check_subscription(commenter_id, draft["access_token"])
                    reply_text = (
                        draft_value(config, "after_subscription_message", "afterSubscriptionMessage", "")
                        if subscribed
                        else draft_value(config, "no_subscription_message", "noSubscriptionMessage", "")
                    ).strip()
                    reply_button = (
                        draft_value(config, "after_subscription_button", "afterSubscriptionButton", "")
                        if subscribed
                        else draft_value(config, "no_subscription_button", "noSubscriptionButton", "")
                    ).strip()
                    reply_url = (
                        draft_value(config, "after_subscription_url", "afterSubscriptionUrl", "")
                        if subscribed
                        else ""
                    ).strip()
                    reply_payload = ""
                    if subscribed:
                        with connection() as db:
                            db.execute(
                                "DELETE FROM pending_subscription_checks WHERE ig_user_id = ? AND sender_id = ? AND draft_id = ?",
                                (ig_user_id, commenter_id, draft["id"]),
                            )
                    else:
                        reply_button = reply_button or draft_value(
                            config, "no_subscription_button", "noSubscriptionButton", "Check follow"
                        ).strip() or "Check follow"
                        reply_payload = f"SA_CHECK_SUBSCRIPTION:{draft['id']}"
                        with connection() as db:
                            db.execute(
                                "INSERT OR REPLACE INTO pending_subscription_checks (ig_user_id, sender_id, draft_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                                (ig_user_id, commenter_id, draft["id"], canonical_session_id, now()),
                            )
                else:
                    reply_text = draft_value(config, "welcome_message", "welcomeMessage", "").strip()
                    reply_button = draft_value(config, "welcome_button", "welcomeButton", "").strip()
                    reply_url = ""
                    reply_payload = ""
                if not reply_text:
                    continue
                comment_replies = [
                    str(message).strip()
                    for message in draft_value(config, "comment_replies", "commentReplies", [])
                    if str(message).strip()
                ]
                if draft_value(config, "comment_replies_enabled", "commentRepliesEnabled", False) and comment_replies:
                    try:
                        await reply_to_comment(
                            comment_id,
                            secrets.choice(comment_replies),
                            draft["access_token"],
                        )
                        logger.info("Instagram automatic comment reply sent: comment_id=%s draft=%s", comment_id, draft["id"])
                    except InstagramError:
                        logger.exception("Could not send automatic comment reply %s", comment_id)
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
                        reply_text,
                        draft["access_token"],
                        reply_button,
                        reply_url,
                        reply_payload,
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
            quick_reply_payload = str((message.get("quick_reply") or {}).get("payload", ""))
            sender_id = str(messaging.get("sender", {}).get("id", ""))
            logger.info(
                "Instagram message event: account=%s sender=%s message_id=%s has_text=%s echo=%s",
                ig_user_id,
                sender_id,
                message_id,
                bool(message_text),
                bool(message.get("is_echo")),
            )
            if not message_id or not sender_id or sender_id == ig_user_id:
                continue

            if quick_reply_payload.startswith("SA_CHECK_SUBSCRIPTION:"):
                draft_id = quick_reply_payload.partition(":")[2]
                with connection() as db:
                    pending = db.execute(
                        """SELECT p.draft_id, p.session_id, d.config_json, s.access_token
                           FROM pending_subscription_checks p
                           JOIN automation_drafts d ON d.id = p.draft_id AND d.enabled = 1
                           JOIN sessions s ON s.id = p.session_id AND s.access_token IS NOT NULL
                           WHERE p.ig_user_id = ? AND p.sender_id = ? AND p.draft_id = ? AND p.session_id = ?""",
                        (ig_user_id, sender_id, draft_id, canonical_session_id),
                    ).fetchone()
                if not pending:
                    logger.info("Ignoring subscription confirmation without pending check: draft=%s sender=%s", draft_id, sender_id)
                    continue
                try:
                    with connection() as db:
                        db.execute(
                            "INSERT INTO processed_draft_events (event_id, draft_id, created_at) VALUES (?, ?, ?)",
                            (message_id, draft_id, now()),
                        )
                except sqlite3.IntegrityError:
                    continue

                config = json.loads(pending["config_json"])
                try:
                    subscribed = await check_subscription(sender_id, pending["access_token"])
                    if subscribed:
                        reply_text = draft_value(config, "after_subscription_message", "afterSubscriptionMessage", "").strip()
                        reply_button = draft_value(config, "after_subscription_button", "afterSubscriptionButton", "").strip()
                        reply_url = draft_value(config, "after_subscription_url", "afterSubscriptionUrl", "").strip()
                        if not reply_text:
                            raise InstagramError("After-subscription message is empty")
                        await send_direct_message(
                            ig_user_id,
                            sender_id,
                            reply_text,
                            pending["access_token"],
                            reply_button,
                            reply_url,
                        )
                        with connection() as db:
                            db.execute(
                                "DELETE FROM pending_subscription_checks WHERE ig_user_id = ? AND sender_id = ? AND draft_id = ?",
                                (ig_user_id, sender_id, draft_id),
                            )
                        logger.info("Subscription confirmed; after-subscription message sent: draft=%s sender=%s", draft_id, sender_id)
                    else:
                        reply_text = draft_value(config, "no_subscription_message", "noSubscriptionMessage", "").strip()
                        reply_button = draft_value(config, "no_subscription_button", "noSubscriptionButton", "Check follow").strip() or "Check follow"
                        if not reply_text:
                            raise InstagramError("Not-subscribed message is empty")
                        await send_direct_message(
                            ig_user_id,
                            sender_id,
                            reply_text,
                            pending["access_token"],
                            reply_button,
                            "",
                            quick_reply_payload,
                        )
                        logger.info("Subscription not detected yet; confirmation remains pending: draft=%s sender=%s", draft_id, sender_id)
                except InstagramError:
                    logger.exception("Could not process subscription confirmation: draft=%s sender=%s", draft_id, sender_id)
                    with connection() as db:
                        db.execute(
                            "DELETE FROM processed_draft_events WHERE event_id = ? AND draft_id = ?",
                            (message_id, draft_id),
                        )
                continue

            if not message_text:
                continue

            # Direct-message rules use the reserved post id. The existing
            # simple automation table is retained for backwards compatibility
            # until the richer automation form is activated server-side.
            with connection() as db:
                matches = db.execute(
                    """SELECT a.id, a.keyword, a.text, s.access_token FROM automations a
                       JOIN sessions s ON a.session_id = s.id
                       WHERE s.ig_user_id = ? AND s.id = ? AND a.post_id = '__direct__'
                         AND a.enabled = 1 AND s.access_token IS NOT NULL""",
                    (ig_user_id, canonical_session_id),
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
                       WHERE s.ig_user_id = ? AND s.id = ? AND d.enabled = 1
                         AND s.access_token IS NOT NULL""",
                    (ig_user_id, canonical_session_id),
                ).fetchall()
            logger.info("Form DM automations available: %d", len(draft_rows))
            for draft in draft_rows:
                config = json.loads(draft["config_json"])
                if "direct" not in draft_value(config, "triggers", "triggers", []):
                    continue
                keywords = [
                    str(keyword).strip()
                    for keyword in draft_value(config, "keywords", "keywords", [])
                    if str(keyword).strip()
                ]
                comment_mode = draft_value(config, "comment_mode", "commentMode", "any")
                exact_match = bool(draft_value(config, "exact_match", "exactMatch", False))
                if comment_mode == "keywords" and not keyword_matches(message_text, keywords, exact_match):
                    logger.info(
                        "Skipping DM automation: draft=%s mode=keywords exact=%s keywords=%s",
                        draft["id"],
                        exact_match,
                        keywords,
                    )
                    continue
                if draft_value(config, "check_subscription", "checkSubscription", False):
                    subscribed = await check_subscription(sender_id, draft["access_token"])
                    reply_text = (
                        draft_value(config, "after_subscription_message", "afterSubscriptionMessage", "")
                        if subscribed
                        else draft_value(config, "no_subscription_message", "noSubscriptionMessage", "")
                    ).strip()
                    reply_button = (
                        draft_value(config, "after_subscription_button", "afterSubscriptionButton", "")
                        if subscribed
                        else draft_value(config, "no_subscription_button", "noSubscriptionButton", "")
                    ).strip()
                    reply_url = (
                        draft_value(config, "after_subscription_url", "afterSubscriptionUrl", "")
                        if subscribed
                        else ""
                    ).strip()
                    reply_payload = ""
                    if subscribed:
                        with connection() as db:
                            db.execute(
                                "DELETE FROM pending_subscription_checks WHERE ig_user_id = ? AND sender_id = ? AND draft_id = ?",
                                (ig_user_id, sender_id, draft["id"]),
                            )
                    else:
                        reply_button = reply_button or "Check follow"
                        reply_payload = f"SA_CHECK_SUBSCRIPTION:{draft['id']}"
                        with connection() as db:
                            db.execute(
                                "INSERT OR REPLACE INTO pending_subscription_checks (ig_user_id, sender_id, draft_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)",
                                (ig_user_id, sender_id, draft["id"], canonical_session_id, now()),
                            )
                else:
                    reply_text = draft_value(config, "welcome_message", "welcomeMessage", "").strip()
                    reply_button = draft_value(config, "welcome_button", "welcomeButton", "").strip()
                    reply_url = ""
                    reply_payload = ""
                if not reply_text:
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
                        reply_text,
                        draft["access_token"],
                        reply_button,
                        reply_url,
                        reply_payload,
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
