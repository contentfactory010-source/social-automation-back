"""Resolve a Content Factory user through its existing authenticated API."""

try:
    import httpx
except ImportError:  # Cloudflare uses the Worker Fetch API instead.
    httpx = None
from fastapi import HTTPException


STAGE_CLIENT_ORIGINS = frozenset({
    "https://content-factory-stage.web.app",
    "https://content-factory-stage.firebaseapp.com",
    "https://saas-dev-eb027.web.app",
    "http://localhost:3000",
    "http://localhost:3007",
})

CONTENT_FACTORY_API_URLS = (
    "https://api.neuro-x.online/api",  # production
    "https://stage.neuro-x.online/api",  # staging
)


def cf_api_base_url(origin: str) -> str:
    return CONTENT_FACTORY_API_URLS[1 if origin in STAGE_CLIENT_ORIGINS else 0]


async def verify_cf_access_token(token: str, origin: str = "") -> str:
    if not token:
        raise HTTPException(401, "Content Factory login is required")
    api_url = cf_api_base_url(origin)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(f"{api_url}/auth/me", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Content Factory identity service is unavailable") from exc
    if response.status_code in (401, 403):
        raise HTTPException(401, "Content Factory login has expired")
    if response.status_code != 200:
        raise HTTPException(503, "Content Factory identity service is unavailable")
    try:
        user_id = response.json().get("user_id")
    except (ValueError, AttributeError) as exc:
        raise HTTPException(503, "Invalid Content Factory identity response") from exc
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(503, "Invalid Content Factory identity response")
    return user_id
