"""Cloudflare Python Worker entrypoint for the existing FastAPI application."""

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
legacy.get_user_profile = cloudflare_instagram.get_user_profile
legacy.reply_to_comment = cloudflare_instagram.reply_to_comment
legacy.send_direct_message = cloudflare_instagram.send_direct_message
legacy.subscribe_to_webhooks = cloudflare_instagram.subscribe_to_webhooks

# FastAPI builds CORSMiddleware before Worker bindings are available. Remove
# that static middleware and apply the Worker origin at request time instead.
legacy.app.user_middleware = [
    middleware
    for middleware in legacy.app.user_middleware
    if middleware.cls.__name__ != "CORSMiddleware"
]
legacy.app.middleware_stack = None


@legacy.app.middleware("http")
async def worker_context(request, call_next):
    token = bind_env(request.scope["env"])
    legacy.FRONTEND_URL = value("FRONTEND_URL", "http://localhost:5173").rstrip("/")
    legacy.COOKIE_SECURE = value("COOKIE_SECURE", "true").lower() == "true"
    legacy.COOKIE_SAMESITE = value("COOKIE_SAMESITE", "none").lower()

    try:
        origin = request.headers.get("origin")
        cors_headers = {}
        if origin and origin.rstrip("/") == legacy.FRONTEND_URL:
            cors_headers = {
                "Access-Control-Allow-Origin": legacy.FRONTEND_URL,
                "Access-Control-Allow-Credentials": "true",
                "Vary": "Origin",
            }

        if request.method == "OPTIONS":
            from starlette.responses import Response

            response = Response(status_code=204)
        else:
            response = await call_next(request)

        for name, header_value in cors_headers.items():
            response.headers[name] = header_value
        if request.method == "OPTIONS" and cors_headers:
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = request.headers.get(
                "access-control-request-headers", "Content-Type"
            )
        return response
    finally:
        reset_env(token)


Default = asgi.entrypoint(legacy.app)
