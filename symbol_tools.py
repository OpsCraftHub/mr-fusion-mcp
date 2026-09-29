"""AI Runner symbol-index lookup tools for the local MCP.

Sibling of rag_tools.py + workspace_tools.py. Exposes the tree-sitter
symbol index (ADR-0001 ticket `9ba78575`) so a Claude Code session
can answer "where is X defined?" without grepping source files.

Mirrors the tools:symbols chat bundle so behaviour is the same from
either surface:
  find_symbol           — every def matching a name
  outline_file          — line-ordered defs in one file
  list_symbols_in_repo  — browse with optional filters

Refs (`find_references` / `callers_of` / `callees_of`) aren't in v1 —
the name index answers 80% of the useful questions and refs need a
separate parser pass.
"""

import json
import os
from typing import Any

import httpx

AI_RUNNER_URL = os.getenv(
    "AI_RUNNER_URL",
    "https://mr-fusion.opscraft.cc/api/ai-runner",
).rstrip("/")


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _error_detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        d = body.get("detail")
        if isinstance(d, dict):
            return f"{d.get('error', 'error')}: {d.get('detail', '')}"
        return str(d or body)
    except Exception:
        return r.text[:200] if r.text else f"HTTP {r.status_code}"


def _raise_for(r: httpx.Response, prefix: str) -> None:
    if r.is_success:
        return
    if r.status_code == 401:
        raise Exception(f"{prefix}: unauthenticated")
    if r.status_code == 403:
        raise Exception(f"{prefix}: forbidden — no access to that org/LP")
    if r.status_code == 422:
        raise Exception(f"{prefix}: bad input — {_error_detail(r)}")
    if r.status_code == 404:
        raise Exception(f"{prefix}: {_error_detail(r)}")
    raise Exception(f"{prefix}: HTTP {r.status_code} — {_error_detail(r)}")


def register_symbol_tools(mcp, auth_headers_fn, org_id_fn):
    """Attach symbol tools to the FastMCP instance. Uses the same
    auth pattern as workspace_tools + rag_tools — org_id_fn resolves
    the caller's org UUID from the KC token."""

    async def _org_prefix(lp_id: str) -> str:
        org_id = await org_id_fn()
        return f"/workspace/orgs/{org_id}/lps/{lp_id}"

    @mcp.tool()
    async def find_symbol(
        lp_id: str,
        name: str,
        repo: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> str:
        """Find every symbol definition matching NAME in the LP's
        indexed repos. Returns file+line+kind+signature+docstring for
        each hit. Zero token cost — pure Postgres lookup over the
        tree-sitter parsed index.

        Matches SHORT name (`start`) OR qualified name (`main.Task.start`).
        For clarity when duplicates exist, prefer qualified form.

        Args:
            lp_id: LP UUID to scope the search.
            name: Symbol name (short or qualified).
            repo: Optional — narrow to one attached repo.
            kind: Optional — filter to function/method/class/struct/
                interface/type/const.
            limit: Max results (1-200, default 50).
        """
        if not name.strip():
            return _fmt({"count": 0, "hits": [], "error": "name required"})
        prefix = await _org_prefix(lp_id)
        params: dict = {"name": name.strip(), "limit": max(1, min(200, limit))}
        if repo:
            params["repo"] = repo
        if kind:
            params["kind"] = kind
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{AI_RUNNER_URL}{prefix}/symbols/find",
                params=params, headers=await auth_headers_fn(), timeout=30,
            )
        _raise_for(r, "find_symbol")
        return _fmt(r.json())

    @mcp.tool()
    async def outline_file(lp_id: str, repo: str, file: str) -> str:
        """List every top-level def in a specific file, ordered by
        line. Cheap way to survey a module without pulling the source
        into your context. Returns just signatures + docstrings, not
        bodies.

        Args:
            lp_id: LP UUID.
            repo: Repo name (as registered).
            file: Repo-relative posix path (e.g. 'services/foo/main.py').
        """
        if not file.strip():
            return _fmt({"count": 0, "symbols": [], "error": "file required"})
        prefix = await _org_prefix(lp_id)
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{AI_RUNNER_URL}{prefix}/repos/{repo}/outline",
                params={"file": file.strip()},
                headers=await auth_headers_fn(), timeout=30,
            )
        _raise_for(r, "outline_file")
        return _fmt(r.json())

    @mcp.tool()
    async def list_symbols_in_repo(
        lp_id: str,
        repo: str | None = None,
        kind: str | None = None,
        language: str | None = None,
        name_prefix: str | None = None,
        limit: int = 100,
    ) -> str:
        """Browse symbol defs in an LP with optional filters. Use for
        'what classes are in ledger-go?' / 'list all interfaces in
        board-go' / 'find every function starting with handle_'.

        Case-sensitive prefix matching on name (SQL wildcards are
        escaped, so a literal % in the prefix won't act as a wildcard).

        Args:
            lp_id: LP UUID.
            repo: Optional — narrow to one repo.
            kind: Optional — function/method/class/struct/interface/
                type/const.
            language: Optional — python/go/typescript/tsx.
            name_prefix: Optional — case-sensitive symbol-name prefix.
            limit: Max results (1-500, default 100).
        """
        prefix = await _org_prefix(lp_id)
        params: dict = {"limit": max(1, min(500, limit))}
        if repo:
            params["repo"] = repo
        if kind:
            params["kind"] = kind
        if language:
            params["language"] = language
        if name_prefix:
            params["name_prefix"] = name_prefix
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{AI_RUNNER_URL}{prefix}/symbols",
                params=params, headers=await auth_headers_fn(), timeout=30,
            )
        _raise_for(r, "list_symbols_in_repo")
        return _fmt(r.json())

    return mcp
