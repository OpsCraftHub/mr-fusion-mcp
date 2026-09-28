"""AI Runner RAG tools — exposes semantic search over the org's
indexed content (tickets + comments + time entries) as MCP tools.

Streaming /rag/chat is deliberately skipped — SSE doesn't fit MCP's
request/response model and the caller is always another LLM that can
reason from search results directly. If a non-streaming /rag/chat/turn
endpoint ships later, add a `rag_ask` tool here that wraps it.
"""

import json
import os
from typing import Any

import httpx

# In-cluster host is only used when the MCP runs inside k8s; local
# execution hits the frontend nginx proxy which forwards to ai-runner.
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
            extra = ""
            if code == "byok_missing":
                missing = d.get("missing") or []
                extra = f" (missing: {', '.join(missing)})"
            return f"{code}: {msg}{extra}"
        return str(d or body)
    except Exception:
        return r.text[:200] if r.text else f"HTTP {r.status_code}"


def register_rag_tools(mcp, auth_headers_fn):
    """Attach RAG tools to the passed FastMCP instance.

    auth_headers_fn: async callable returning {"Authorization": "Bearer ..."}.
    Reuses the dispatch server's auth flow so a single KC login covers
    every OpsCraft tool.
    """

    @mcp.tool()
    async def rag_search(
        q: str,
        top_k: int = 10,
        service: str | None = None,
        source_type: str | None = None,
        project_id: str | None = None,
    ) -> str:
        """Semantic search over the org's indexed content.

        Answers "where did we discuss X?" / "have we solved something
        like this before?" / "what did I work on this week?" style
        questions by returning the top-K most-relevant ticket + time
        entry chunks with score + text + deep-link back to the source.

        Access filter (enforced server-side by ai-runner):
          - Hard org boundary from the caller's JWT
          - Project scoped to team_projects unless owner/admin
          - Draft tickets visible only to the creator
          - Time entries visible only to the entry owner unless admin+

        Args:
            q: Natural-language query — full sentences work fine.
            top_k: Number of results to return (1-50, default 10).
            service: Optional filter — e.g. "board" or "chrono" to
                narrow the source. Empty = all indexed services.
            source_type: Optional filter — e.g. "ticket", "comment",
                "op_criterion", "time_entry".
            project_id: Optional filter — narrow to a specific project.
                Intersected with the caller's access rights.

        Returns 412 if the org hasn't configured its BYOK keys yet
        (Settings → Organization → AI Config).
        """
        payload: dict = {"q": q, "top_k": max(1, min(50, top_k))}
        if service:
            payload["service"] = [service]
        if source_type:
            payload["source_type"] = [source_type]
        if project_id:
            payload["project_id"] = [project_id]

        headers = {"Content-Type": "application/json"}
        headers.update(await auth_headers_fn())

        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{AI_RUNNER_URL}/rag/search",
                json=payload,
                headers=headers,
                timeout=30,
            )
        if r.status_code == 412:
            # BYOK gate — surface the specific keys the operator needs
            # to set rather than a generic error, so the caller knows
            # exactly what to fix.
            raise Exception(f"rag_search: {_error_detail(r)}")
        if r.status_code == 403:
            raise Exception(
                f"rag_search: forbidden — the calling user needs the "
                f"`chat` capability granted via Settings → Members → "
                f"AI Access. Detail: {_error_detail(r)}"
            )
        if not r.is_success:
            raise Exception(
                f"rag_search HTTP {r.status_code}: {_error_detail(r)}"
            )
        body = r.json()
        return _fmt({
            "query": body.get("query"),
            "count": body.get("count"),
            "results": body.get("results", []),
        })

    return mcp
