"""Cloudflare Python Worker entrypoint for the existing FastAPI application."""

from fastapi.middleware.cors import CORSMiddleware
from workers import asgi

import main as legacy
import cloudflare_db
import cloudflare_instagram
from cloudflare_config import bind_env, reset_env, setting, value


# Keep the route implementation in main.py while replacing platform services.
legacy.connection = cloudflare_db.connection
legacy.init_db = cloudflare_db.init_db
legacy.sqlite3.IntegrityError = cloudflare_db.IntegrityError
legacy.setting = setting
legacy.authorization_url = cloudflare_instagram.authorization_url
legacy.exchange_code = cloudflare_instagram.exchange_code
legacy.get_posts = cloudflare_instagram.get_posts
legacy.get_profile = cloudflare_instagram.get_profile
legacy.reply_to_comment = cloudflare_instagram.reply_to_comment
legacy.send_direct_message = cloudflare_instagram.send_direct_message
legacy.subscribe_to_webhooks = cloudflare_instagram.subscribe_to_webhooks


@legacy.app.middleware("http")
async def worker_context(request, call_next):
    token = bind_env(request.scope["env"])
    legacy.FRONTEND_URL = value("FRONTEND_URL", "http://localhost:5173").rstrip("/")
    legacy.COOKIE_SECURE = value("COOKIE_SECURE", "true").lower() == "true"
    legacy.COOKIE_SAMESITE = value("COOKIE_SAMESITE", "none").lower()

    # CORS is configured after Worker bindings become available.
    for middleware in legacy.app.user_middleware:
        if middleware.cls is CORSMiddleware:
            middleware.kwargs["allow_origins"] = [legacy.FRONTEND_URL]
            break
    try:
        return await call_next(request)
    finally:
        reset_env(token)


Default = asgi.entrypoint(legacy.app)
