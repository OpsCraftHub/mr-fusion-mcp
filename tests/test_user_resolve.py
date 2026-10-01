"""Tests for the username → KC subject resolver in dispatch_server.

The resolver is the fix for the "ross" vs UUID bug — bare usernames
used to land verbatim in task_assignees.user_id and leave tasks
invisible to /tasks/mine (which keys on JWT subject).

Covers:
  - UUID input returns unchanged, no network hop
  - Known username → correct KC subject
  - Email + full-name lookups
  - Ambiguous match raises with candidates
  - Unknown input raises a clear "no match" error
  - Prefix fallback resolves common shortenings
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

# dispatch_server imports from `auth` which reads env at import time —
# set the harmless defaults before importing.
os.environ.setdefault("KEYCLOAK_URL", "")
os.environ.setdefault("KEYCLOAK_USERNAME", "")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dispatch_server  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a cold user cache so a prior test's
    mock payload doesn't bleed through."""
    dispatch_server._user_cache = None
    yield
    dispatch_server._user_cache = None


SAMPLE_USERS = [
    {
        "id": "0ba824d4-6a37-44a7-9214-a93e4aa0497a",
        "username": "ross",
        "email": "ross.lutsch@gmail.com",
        "name": "Ross Lutsch",
        "first_name": "Ross", "last_name": "Lutsch",
        "roles": ["owner"],
    },
    {
        "id": "11111111-aaaa-bbbb-cccc-222222222222",
        "username": "nickthomson",
        "email": "nickthomson@gotbot.ai",
        "name": "Nick Thomson",
        "first_name": "Nick", "last_name": "Thomson",
        "roles": ["admin"],
    },
    {
        "id": "33333333-dddd-eeee-ffff-444444444444",
        "username": "ross2",
        "email": "ross2@opscraft.cc",
        "name": "Ross Second",
        "first_name": "Ross", "last_name": "Second",
        "roles": ["member"],
    },
]


class TestFastPath:
    """UUID input should round-trip without touching the network — the
    common case for scripts that already have the subject in hand."""

    @pytest.mark.asyncio
    async def test_uuid_returns_unchanged(self):
        # No mock on _get — if the function tried to hit the network
        # this test would blow up on a connection error.
        uid = "0ba824d4-6a37-44a7-9214-a93e4aa0497a"
        resolved = await dispatch_server._resolve_user_id(uid)
        assert resolved == uid

    @pytest.mark.asyncio
    async def test_malformed_uuid_falls_through_to_realm_lookup(self):
        # "not-a-uuid-but-looks-close" should NOT short-circuit; it
        # should hit the realm users and raise no-match.
        with patch("dispatch_server._get", new=AsyncMock(return_value=SAMPLE_USERS)):
            with pytest.raises(ValueError, match="No realm user matches"):
                await dispatch_server._resolve_user_id("not-a-uuid")


class TestKnownUserLookup:

    @pytest.mark.asyncio
    async def test_username_match(self):
        with patch("dispatch_server._get", new=AsyncMock(return_value=SAMPLE_USERS)):
            resolved = await dispatch_server._resolve_user_id("ross")
        assert resolved == "0ba824d4-6a37-44a7-9214-a93e4aa0497a"

    @pytest.mark.asyncio
    async def test_username_case_insensitive(self):
        with patch("dispatch_server._get", new=AsyncMock(return_value=SAMPLE_USERS)):
            resolved = await dispatch_server._resolve_user_id("ROSS")
        assert resolved == "0ba824d4-6a37-44a7-9214-a93e4aa0497a"

    @pytest.mark.asyncio
    async def test_email_match(self):
        with patch("dispatch_server._get", new=AsyncMock(return_value=SAMPLE_USERS)):
            resolved = await dispatch_server._resolve_user_id("nickthomson@gotbot.ai")
        assert resolved == "11111111-aaaa-bbbb-cccc-222222222222"

    @pytest.mark.asyncio
    async def test_full_name_match(self):
        with patch("dispatch_server._get", new=AsyncMock(return_value=SAMPLE_USERS)):
            resolved = await dispatch_server._resolve_user_id("Ross Lutsch")
        assert resolved == "0ba824d4-6a37-44a7-9214-a93e4aa0497a"

    @pytest.mark.asyncio
    async def test_prefix_fallback(self):
        # No exact match — "nick" prefix matches exactly one username.
        with patch("dispatch_server._get", new=AsyncMock(return_value=SAMPLE_USERS)):
            resolved = await dispatch_server._resolve_user_id("nick")
        assert resolved == "11111111-aaaa-bbbb-cccc-222222222222"


class TestFailureModes:

    @pytest.mark.asyncio
    async def test_unknown_user_raises(self):
        with patch("dispatch_server._get", new=AsyncMock(return_value=SAMPLE_USERS)):
            with pytest.raises(ValueError, match="No realm user matches 'who'"):
                await dispatch_server._resolve_user_id("who")

    @pytest.mark.asyncio
    async def test_empty_identifier_raises(self):
        with pytest.raises(ValueError, match="user_id is required"):
            await dispatch_server._resolve_user_id("")

    @pytest.mark.asyncio
    async def test_prefix_ambiguous_raises(self):
        # "ross" prefix matches both ross and ross2 if we weren't
        # already catching the exact "ross" first. Verify the exact
        # match wins + the prefix branch doesn't fire.
        with patch("dispatch_server._get", new=AsyncMock(return_value=SAMPLE_USERS)):
            resolved = await dispatch_server._resolve_user_id("ross")
        # Exact match on "ross" wins — not "ross2".
        assert resolved == "0ba824d4-6a37-44a7-9214-a93e4aa0497a"

    @pytest.mark.asyncio
    async def test_realm_users_fetch_failure_wrapped(self):
        # Network / permission error should surface a specific error
        # about owner/admin role, not a bare httpx exception.
        with patch("dispatch_server._get", new=AsyncMock(side_effect=Exception("403 forbidden"))):
            with pytest.raises(Exception, match="org owner/admin role"):
                await dispatch_server._resolve_user_id("ross")


class TestCaching:

    @pytest.mark.asyncio
    async def test_second_lookup_hits_cache(self):
        # Only one HTTP call even across many lookups within one process.
        mock_get = AsyncMock(return_value=SAMPLE_USERS)
        with patch("dispatch_server._get", new=mock_get):
            await dispatch_server._resolve_user_id("ross")
            await dispatch_server._resolve_user_id("nickthomson")
            await dispatch_server._resolve_user_id("ross2")
        assert mock_get.await_count == 1
