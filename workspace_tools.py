"""AI Runner workspace tools — LP repo lifecycle from the local MCP.

Sibling of rag_tools.py — same auth flow, same base URL. Exposes the
Repo Onboarding Pipeline (ADR-0001 tickets `dc046a22` + `d2b9c072`)
so a Claude Code session can:
  - register a repo into an LP → get the RepoSurvey back
  - list attached repos with their remote_url + head_sha
  - remove a repo (wipes clone + symbol_defs + survey)
  - fetch latest + rescan
  - rescan without fetching
  - read the persisted RepoSurvey
  - trigger a symbol reindex

Every tool hits an admin-gated endpoint on ai-runner. The caller's KC
credentials (from the dispatch_server auth flow) must belong to a user
with owner / org-admin / goduser role on the target org.
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
    """Structured errors from ai-runner have {detail: {error, detail, ...}}."""
    try:
        body = r.json()
        d = body.get("detail")
        if isinstance(d, dict):
            code = d.get("error") or "error"
            msg = d.get("detail") or code
            return f"{code}: {msg}"
        return str(d or body)
    except Exception:
        return r.text[:200] if r.text else f"HTTP {r.status_code}"


def _raise_for(r: httpx.Response, prefix: str) -> None:
    """Turn a non-2xx into an Exception the MCP surfaces cleanly."""
    if r.is_success:
        return
    if r.status_code == 401:
        raise Exception(f"{prefix}: unauthenticated — KC token may be expired")
    if r.status_code == 403:
        raise Exception(
            f"{prefix}: forbidden — caller needs owner/admin role on the target org"
        )
    if r.status_code == 404:
        raise Exception(f"{prefix}: {_error_detail(r)}")
    if r.status_code == 409:
        raise Exception(f"{prefix}: conflict — {_error_detail(r)}")
    if r.status_code == 422:
        raise Exception(f"{prefix}: bad input — {_error_detail(r)}")
    raise Exception(f"{prefix}: HTTP {r.status_code} — {_error_detail(r)}")


def register_workspace_tools(mcp, auth_headers_fn, org_id_fn):
    """Attach workspace tools to the FastMCP instance.

    auth_headers_fn: async callable returning {"Authorization": "Bearer ..."}.
    org_id_fn:       async callable returning the caller's org UUID (from
                     the same Keycloak flow). Every tool uses this — the
                     endpoints are org-scoped in the URL.
    """

    async def _org_path_prefix(lp_id: str) -> str:
        org_id = await org_id_fn()
        return f"/workspace/orgs/{org_id}/lps/{lp_id}"

    @mcp.tool()
    async def workspace_register_repo(
        lp_id: str,
        git_url: str,
        repo_name: str,
        branch: str | None = None,
    ) -> str:
        """Clone a repo into the LP's workspace + run the structural
        scanner + build the symbol index.

        Returns the RepoSurvey (languages, services, existing docs +
        ADRs, AI-context files) plus a symbols summary (defs_written
        + by-kind counts).

        Requires owner/admin role on the org. 409 if the repo name is
        already attached (use workspace_remove_repo first). 502 if
        the git clone fails (bad URL, auth issues).

        Args:
            lp_id: LP UUID to attach the repo to.
            git_url: Cloneable git URL (ssh:// or https://).
            repo_name: Short name — used for the on-disk dir + stable
                doc IDs. [A-Za-z0-9][A-Za-z0-9._-]{0,62}
            branch: Optional — omit for the repo's default branch.
        """
        body: dict = {"git_url": git_url, "repo_name": repo_name}
        if branch:
            body["branch"] = branch
        prefix = await _org_path_prefix(lp_id)
        headers = {"Content-Type": "application/json"}
        headers.update(await auth_headers_fn())
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{AI_RUNNER_URL}{prefix}/repos", json=body,
                headers=headers, timeout=120,
            )
        _raise_for(r, "workspace_register_repo")
        return _fmt(r.json())

    @mcp.tool()
    async def workspace_list_repos(lp_id: str) -> str:
        """List repos attached to this LP. Each entry carries name,
        on-disk path, remote_url, and current HEAD sha. Read-only —
        any org member with visibility to the LP can call this.
        """
        prefix = await _org_path_prefix(lp_id)
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{AI_RUNNER_URL}{prefix}/repos",
                headers=await auth_headers_fn(), timeout=30,
            )
        _raise_for(r, "workspace_list_repos")
        return _fmt(r.json())

    @mcp.tool()
    async def workspace_remove_repo(lp_id: str, repo_name: str) -> str:
        """Remove a repo from the LP workspace. Deletes: cloned working
        tree, persisted RepoSurvey, symbol_defs rows. RAG chunks tied
        to this repo's docs stay (they live in rag_ticket_chunks, not
        this pipeline's tables). Idempotent — 204 whether the repo was
        attached or not."""
        prefix = await _org_path_prefix(lp_id)
        async with httpx.AsyncClient() as c:
            r = await c.delete(
                f"{AI_RUNNER_URL}{prefix}/repos/{repo_name}",
                headers=await auth_headers_fn(), timeout=30,
            )
        _raise_for(r, "workspace_remove_repo")
        return _fmt({"removed": repo_name, "status": r.status_code})

    @mcp.tool()
    async def workspace_fetch_repo(lp_id: str, repo_name: str) -> str:
        """Pull latest from origin + re-run the structural scanner.
        Returns the new HEAD sha + refreshed survey. 404 if the repo
        isn't attached."""
        prefix = await _org_path_prefix(lp_id)
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{AI_RUNNER_URL}{prefix}/repos/{repo_name}/fetch",
                headers=await auth_headers_fn(), timeout=120,
            )
        _raise_for(r, "workspace_fetch_repo")
        return _fmt(r.json())

    @mcp.tool()
    async def workspace_rescan_repo(lp_id: str, repo_name: str) -> str:
        """Re-run the scanner against the currently-cloned tree without
        fetching from remote. Cheap way to refresh a survey after
        manually editing a config file on the pod's PVC.
        """
        prefix = await _org_path_prefix(lp_id)
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{AI_RUNNER_URL}{prefix}/repos/{repo_name}/rescan",
                headers=await auth_headers_fn(), timeout=60,
            )
        _raise_for(r, "workspace_rescan_repo")
        return _fmt(r.json())

    @mcp.tool()
    async def workspace_get_survey(lp_id: str, repo_name: str) -> str:
        """Read the last-persisted RepoSurvey for this repo. 404 if
        never scanned. Read-only — member-accessible."""
        prefix = await _org_path_prefix(lp_id)
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{AI_RUNNER_URL}{prefix}/repos/{repo_name}/survey",
                headers=await auth_headers_fn(), timeout=30,
            )
        _raise_for(r, "workspace_get_survey")
        return _fmt(r.json())

    @mcp.tool()
    async def trigger_doc_backfill() -> str:
        """Kick off a bulk re-ingest of every Lattice document in the
        caller's org into the RAG. Same button as Settings → Organization
        → AI Config → 'Ingest all Lattice documents', callable from
        Claude Code so the setup flow can happen entirely from your
        terminal.

        Returns immediately with a job snapshot; poll doc_backfill_status
        to watch progress. 202 on accept, 409 if one is already running,
        412 if BYOK embeddings keys aren't configured.

        Requires owner/admin role on the org.
        """
        org_id = await org_id_fn()
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{AI_RUNNER_URL}/rag/backfill/{org_id}/documents",
                headers=await auth_headers_fn(), timeout=30,
            )
        _raise_for(r, "trigger_doc_backfill")
        return _fmt(r.json())

    @mcp.tool()
    async def doc_backfill_status() -> str:
        """Read the latest state of the Lattice doc backfill for the
        caller's org. Non-blocking — returns immediately with counts
        and status (running / completed / failed / cancelled). 404 if
        no backfill has been kicked off on this pod.
        """
        org_id = await org_id_fn()
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{AI_RUNNER_URL}/rag/backfill/{org_id}/documents/status",
                headers=await auth_headers_fn(), timeout=15,
            )
        _raise_for(r, "doc_backfill_status")
        return _fmt(r.json())

    @mcp.tool()
    async def workspace_reindex_symbols(lp_id: str, repo_name: str) -> str:
        """Rebuild the tree-sitter symbol_defs index for this repo from
        the current working tree. Wipes + re-inserts atomically.
        Returns {files_seen, files_parsed, files_skipped, defs_written,
        defs_by_kind, duration_ms}. 404 if the repo isn't attached."""
        prefix = await _org_path_prefix(lp_id)
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{AI_RUNNER_URL}{prefix}/repos/{repo_name}/reindex-symbols",
                headers=await auth_headers_fn(), timeout=300,
            )
        _raise_for(r, "workspace_reindex_symbols")
        return _fmt(r.json())

    return mcp
