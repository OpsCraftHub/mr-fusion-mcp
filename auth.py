"""Shared Keycloak auth for MCP servers."""

import os
import time
import warnings
from typing import Any, Callable, Coroutine

import httpx

# FastMCP's Settings model has a `lifespan` field whose annotation contains an
# unresolved forward reference; pydantic_settings >= 2.6 warns on import. The
# warning is harmless but pollutes stderr and can confuse MCP clients.
warnings.filterwarnings(
    "ignore",
    message=r"Field 'lifespan' has an incomplete definition.*",
)

_token_caches: dict[str, dict[str, Any]] = {}


async def get_token(
    kc_url: str, realm: str, client_id: str, username: str, password: str,
) -> str:
    """Return a valid Bearer token via Keycloak password grant."""
    cache_key = f"{kc_url}:{realm}:{username}"
    cache = _token_caches.setdefault(cache_key, {"access_token": "", "expires_at": 0})

    if cache["access_token"] and time.time() < cache["expires_at"] - 30:
        return cache["access_token"]

    token_url = f"{kc_url}/realms/{realm}/protocol/openid-connect/token"
    async with httpx.AsyncClient() as c:
        r = await c.post(token_url, data={
            "grant_type": "password",
            "client_id": client_id,
            "username": username,
            "password": password,
        }, timeout=10)
        r.raise_for_status()
        data = r.json()

    cache["access_token"] = data["access_token"]
    cache["expires_at"] = time.time() + data.get("expires_in", 300)
    return data["access_token"]


def make_auth_headers_fn(
    static_token_var: str = "BOARD_TOKEN",
) -> Callable[[], Coroutine[Any, Any, dict[str, str]]]:
    """Create an async auth_headers() function from environment variables.

    Reads: KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID,
           KEYCLOAK_USERNAME, KEYCLOAK_PASSWORD
    Falls back to a static token from the env var named by static_token_var.
    """
    kc_url = os.getenv("KEYCLOAK_URL", "")
    kc_realm = os.getenv("KEYCLOAK_REALM", "opscraft")
    kc_client_id = os.getenv("KEYCLOAK_CLIENT_ID", "mr-fusion-frontend")
    kc_username = os.getenv("KEYCLOAK_USERNAME", "")
    kc_password = os.getenv("KEYCLOAK_PASSWORD", "")
    static_token = os.getenv(static_token_var, "")

    async def _auth_headers() -> dict[str, str]:
        if kc_url and kc_username:
            token = await get_token(kc_url, kc_realm, kc_client_id, kc_username, kc_password)
        else:
            token = static_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    return _auth_headers


def make_org_id_fn(
    auth_headers_fn: Callable[[], Coroutine[Any, Any, dict[str, str]]],
) -> Callable[[], Coroutine[Any, Any, str]]:
    """Return an async org_id() function that decodes the KC JWT
    (without signature verification — ai-runner verifies server-side)
    and returns the `org_id` claim.

    Used by workspace + symbol tools where the endpoint URL is scoped
    by org — /workspace/orgs/{org_id}/lps/{lp}/... — so the tool needs
    to resolve the caller's org before making the call.

    Uses stdlib b64 + json (no PyJWT dep). Result cached per token so
    typical use doesn't decode on every call.
    """
    import base64
    import json as _json

    async def _org_id() -> str:
        headers = await auth_headers_fn()
        bearer = headers.get("Authorization", "")
        if not bearer.startswith("Bearer "):
            raise RuntimeError(
                "no Bearer token available — set KEYCLOAK_USERNAME / "
                "KEYCLOAK_PASSWORD or the fallback static token env var"
            )
        token = bearer[len("Bearer "):]
        # JWT = header.payload.signature — decode the middle segment.
        parts = token.split(".")
        if len(parts) != 3:
            raise RuntimeError("bearer isn't a JWT (expected 3 dot-segments)")
        # base64url pad. Python's b64decode is strict about padding.
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception as e:
            raise RuntimeError(f"couldn't decode JWT payload: {e}") from e
        org_id = payload.get("org_id") or payload.get("organization_id")
        if not org_id:
            raise RuntimeError(
                "JWT has no org_id claim — token belongs to a user with "
                "no active org, or Keycloak isn't mapping the org claim "
                "onto this client's tokens"
            )
        return str(org_id)

    return _org_id
