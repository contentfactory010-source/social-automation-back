"""Worker Fetch implementation of Content Factory identity verification."""

from fastapi import HTTPException
from workers import fetch

from cf_bridge import cf_api_base_url


async def verify_cf_access_token(token: str, origin: str = "") -> str:
    if not token:
        raise HTTPException(401, "Content Factory login is required")
    api_url = cf_api_base_url(origin)
    try:
        response = await fetch(f"{api_url}/auth/me", headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:
        raise HTTPException(503, "Content Factory identity service is unavailable") from exc
    if response.status in (401, 403):
        raise HTTPException(401, "Content Factory login has expired")
    if response.status != 200:
        raise HTTPException(503, "Content Factory identity service is unavailable")
    try:
        user_id = (await response.json()).get("user_id")
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(503, "Invalid Content Factory identity response") from exc
    if not isinstance(user_id, str) or not user_id:
        raise HTTPException(503, "Invalid Content Factory identity response")
    return user_id
