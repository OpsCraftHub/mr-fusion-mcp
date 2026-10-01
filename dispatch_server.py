"""Dispatch — MCP server for the OpsCraft Board service.

Exposes board CRUD as MCP tools so Claude Code / Claude Desktop
can read and manage projects, Ops, and packets.
"""

import datetime as _dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from auth import make_auth_headers_fn

# Board-go serves at /api/board-go behind the frontend nginx proxy
# (which rewrites to /api/v1 internally). The old Python `/api/board`
# service is scaled to 0; keeping that as a default would silently
# break every tool invocation.
BOARD_URL = os.getenv("BOARD_URL", "https://mr-fusion.opscraft.cc/api/board-go")

mcp = FastMCP("dispatch")

_auth_headers = make_auth_headers_fn(static_token_var="BOARD_TOKEN")


# ── HTTP helpers ─────────────────────────────────────────────


def _error_detail(r: httpx.Response) -> str:
    """Extract a human-readable error from an HTTP response."""
    try:
        data = r.json()
        return data.get("detail", data.get("error", str(data)))
    except Exception:
        return r.text[:200] if r.text else f"HTTP {r.status_code}"


async def _get(path: str, params: dict | None = None) -> Any:
    headers = await _auth_headers()
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BOARD_URL}{path}", params=params, headers=headers, timeout=30)
        if not r.is_success:
            raise Exception(f"Client error '{r.status_code} {r.reason_phrase}' for url '{r.url}'\n{_error_detail(r)}")
        return r.json()


async def _post(path: str, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    headers.update(await _auth_headers())
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BOARD_URL}{path}", json=body or {}, headers=headers, timeout=30)
        if not r.is_success:
            raise Exception(f"Client error '{r.status_code} {r.reason_phrase}' for url '{r.url}'\n{_error_detail(r)}")
        return r.json()


async def _put(path: str, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    headers.update(await _auth_headers())
    async with httpx.AsyncClient() as c:
        r = await c.put(f"{BOARD_URL}{path}", json=body or {}, headers=headers, timeout=30)
        if not r.is_success:
            raise Exception(f"Client error '{r.status_code} {r.reason_phrase}' for url '{r.url}'\n{_error_detail(r)}")
        return r.json()


async def _delete(path: str) -> str:
    headers = await _auth_headers()
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{BOARD_URL}{path}", headers=headers, timeout=30)
        if not r.is_success:
            raise Exception(f"Client error '{r.status_code} {r.reason_phrase}' for url '{r.url}'\n{_error_detail(r)}")
        return "ok"


def _fmt(data: Any) -> str:
    """Format API response as readable JSON."""
    return json.dumps(data, indent=2, default=str)


# ── User id resolution ───────────────────────────────────────
#
# Problem: bare usernames like "ross" passed to assign_task used to
# land verbatim in task_assignees.user_id. Board queries /tasks/mine
# by the caller's KC subject UUID, so a username-keyed row is invisible
# to the person it was assigned to. 12 "ross"-keyed rows were stranded
# in prod before we noticed (patched manually via kubectl at the time).
#
# This resolver walks board-go's /settings/users (realm user list,
# owner/admin only) and matches the caller-supplied value against:
#   - KC subject UUID (fast path — accepts UUIDs unchanged)
#   - username (case-insensitive)
#   - email (case-insensitive)
#   - full name (first + last concatenated, case-insensitive)
#
# Clear error on no-match / ambiguous-match (prod regret was 200 OK
# with silently-bad data; loudly refusing is better).

import re
import uuid as _uuid

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_user_cache: dict[str, list[dict]] | None = None


def _is_uuid(value: str) -> bool:
    """Cheap check — board-go stores KC subject as a UUID so any input
    that parses as a UUID is already in the right shape. Avoids a
    network hop on the common case."""
    if not value or not _UUID_RE.match(value):
        return False
    try:
        _uuid.UUID(value)
        return True
    except ValueError:
        return False


async def _list_realm_users() -> list[dict]:
    """Fetch + memoise the realm user list. Owner/admin only — if the
    MCP's token lacks the role we fail loud (so the caller sees a
    specific permission error instead of a mystery "user not found")."""
    global _user_cache
    if _user_cache is not None:
        return _user_cache["users"]
    try:
        data = await _get("/settings/users")
    except Exception as e:
        raise Exception(
            f"Could not list realm users to resolve a bare username. "
            f"The MCP's bound user needs org owner/admin role on "
            f"/settings/users. Underlying error: {e}"
        ) from e
    users = data if isinstance(data, list) else data.get("items", [])
    _user_cache = {"users": users}
    return users


async def _resolve_user_id(identifier: str) -> str:
    """Return the KC subject UUID for an identifier that might be a
    UUID, username, email, or full name.

    - UUID → passed through as-is (fast path, no network hop)
    - username / email / full name match (case-insensitive) → UUID
    - 0 matches → raise with a short "no user matches X" message
    - >1 matches → raise with the candidate list so caller can pick
    """
    identifier = (identifier or "").strip()
    if not identifier:
        raise ValueError("user_id is required")
    if _is_uuid(identifier):
        return identifier

    users = await _list_realm_users()
    needle = identifier.lower()

    # Priority order: exact username → exact email → exact name →
    # fallback case-insensitive substring. Only count exact matches
    # for ambiguity detection; substring matches that disagree are
    # a sign the caller should be more specific.
    exact: list[dict] = []
    for u in users:
        if (u.get("username", "") or "").lower() == needle:
            exact.append(u); continue
        if (u.get("email", "") or "").lower() == needle:
            exact.append(u); continue
        if (u.get("name", "") or "").lower() == needle:
            exact.append(u); continue

    if len(exact) == 1:
        return exact[0]["id"]
    if len(exact) > 1:
        candidates = ", ".join(f"{u.get('name') or u.get('username')} ({u['id']})" for u in exact)
        raise ValueError(
            f"Ambiguous user '{identifier}' — multiple matches: {candidates}. "
            f"Pass the UUID directly to disambiguate."
        )

    # No exact match — try prefix on username so "ro" → "ross" works.
    prefix = [u for u in users if (u.get("username", "") or "").lower().startswith(needle)]
    if len(prefix) == 1:
        return prefix[0]["id"]

    raise ValueError(
        f"No realm user matches '{identifier}'. Use list_members() to find "
        f"the right username or UUID."
    )


def _invalidate_user_cache() -> None:
    """Call after any realm-user mutation (none exist in this MCP yet,
    but keeping the hook so future create_user tools can reset)."""
    global _user_cache
    _user_cache = None


# ── Read Tools ───────────────────────────────────────────────


@mcp.tool()
async def create_project(name: str, description: str = "") -> str:
    """Create a new project (Launch Pad).

    Args:
        name: Project name
        description: Optional project description
    """
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    data = await _post("/projects", body)
    return f"Created project: {data['name']} (id: {data['id']})"


@mcp.tool()
async def list_projects() -> str:
    """List all projects (Launch Pads) on the board."""
    data = await _get("/projects")
    projects = data if isinstance(data, list) else data.get("items", data)
    lines = []
    for p in projects:
        pct = p.get("progress_pct", 0)
        lines.append(f"- {p['name']} [{p['status']}] {pct}% — id: {p['id']}")
    return "\n".join(lines) if lines else "No projects found."


@mcp.tool()
async def get_board(project_id: str) -> str:
    """Get the full kanban board for a project — all columns with their packets.

    Args:
        project_id: UUID of the project
    """
    data = await _get(f"/projects/{project_id}/board")
    lines = [f"# {data['project']['name']}"]
    for col in data.get("columns", []):
        state = col["state"]
        tasks = col["tasks"]
        lines.append(f"\n## {state['name']} ({len(tasks)})")
        for t in tasks:
            flags = []
            if t.get("is_blocked"):
                flags.append("BLOCKED")
            if t.get("scope_flag") == "scope_creep":
                flags.append("SCOPE+")
            if t.get("scope_flag") == "possible_scope_creep":
                flags.append("?SCOPE")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            assignee = t.get("assignee_name", "")
            assignee_str = f" @{assignee}" if assignee else ""
            lines.append(f"  - {t['title']}{flag_str}{assignee_str} (id: {t['id']})")
    return "\n".join(lines)


@mcp.tool()
async def list_ops(project_id: str) -> str:
    """List all Ops (sub-projects/deliverables) for a project.

    Args:
        project_id: UUID of the project
    """
    data = await _get(f"/projects/{project_id}/sub-projects")
    ops = data if isinstance(data, list) else data.get("items", data)
    lines = []
    for op in ops:
        approved = " [APPROVED]" if op.get("approved_at") else ""
        scope = ""
        sc = op.get("scope_creep_count", 0)
        psc = op.get("possible_scope_creep_count", 0)
        if sc or psc:
            scope = f" (scope: {sc} creep, {psc} unresolved)"
        dates = ""
        if op.get("planned_start") or op.get("planned_end"):
            dates = f" | {op.get('planned_start', '?')}→{op.get('planned_end', '?')}"
        lines.append(
            f"- {op['name']} [{op['status']}] {op.get('progress_pct', 0)}% "
            f"({op.get('done_tasks', 0)}/{op.get('total_tasks', 0)}){approved}{scope}{dates} — id: {op['id']}"
        )
    return "\n".join(lines) if lines else "No Ops found."


@mcp.tool()
async def list_tasks(
    project_id: str,
    status: str = "",
    assignee: str = "",
    op_id: str = "",
    limit: int = 50,
    created_after: str = "",
    created_before: str = "",
    completed_after: str = "",
    completed_before: str = "",
) -> str:
    """List packets (tasks) in a project with optional filters.

    Args:
        project_id: UUID of the project
        status: Filter by workflow status (e.g. sprint, in_progress, triage)
        assignee: Filter by assignee user ID
        op_id: Filter by Op (sub-project) ID
        limit: Max results (default 50)
        created_after: Only tasks created at or after this date (YYYY-MM-DD or RFC3339). SAST-anchored.
        created_before: Only tasks created strictly before this date. SAST-anchored.
        completed_after: Only tasks completed (done) at or after this date.
        completed_before: Only tasks completed strictly before this date.
    """
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if assignee:
        params["assignee"] = assignee
    if op_id:
        params["sub_project_id"] = op_id
    for key, val in (
        ("created_after", created_after),
        ("created_before", created_before),
        ("completed_after", completed_after),
        ("completed_before", completed_before),
    ):
        if val:
            params[key] = val
    data = await _get(f"/projects/{project_id}/tasks", params)
    tasks = data.get("items", data) if isinstance(data, dict) else data
    lines = []
    for t in tasks:
        flags = []
        if t.get("is_blocked"):
            flags.append("BLOCKED")
        if t.get("scope_flag"):
            flags.append(t["scope_flag"])
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"- [{t['workflow_status']}] {t['title']}{flag_str} "
            f"(priority: {t['priority']}, id: {t['id']})"
        )
    return "\n".join(lines) if lines else "No packets found."


@mcp.tool()
async def get_throughput_stats(
    metric: str = "both",
    range: str = "week",
    from_date: str = "",
    to_date: str = "",
    project_id: str = "",
    op_id: str = "",
) -> str:
    """Answer 'how many tickets came in / shipped in period X?' with a
    by-day breakdown. Use this when asked about intake volume, tickets
    created/completed today, this week, this month, or a custom window.

    Args:
        metric: created | completed | both (default both)
        range: Preset window — day (today), week (WTD), month (MTD),
               prev_week, prev_month. Ignored if from_date/to_date given.
        from_date: Explicit window start, YYYY-MM-DD (SAST midnight) or RFC3339.
                   Requires to_date.
        to_date: Explicit window end, exclusive. Requires from_date.
        project_id: Scope to one project (UUID). Omit for the whole org.
        op_id: Scope to one Op / sub-project (UUID).
    """
    params: dict[str, Any] = {"metric": metric}
    if from_date or to_date:
        # Backend enforces "both or neither" — pass through as-is so the
        # user sees the backend's own 400 message if they mis-supply.
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
    else:
        params["range"] = range
    if project_id:
        params["project_id"] = project_id
    if op_id:
        params["sub_project_id"] = op_id

    data = await _get("/tasks/throughput-stats", params)

    rng = data.get("range", {})
    lines = [
        f"Throughput — {rng.get('preset', '?')} ({rng.get('from', '?')} → {rng.get('to', '?')})",
    ]

    def _render_series(label: str, series: dict[str, Any]) -> None:
        lines.append(f"\n{label}: {series.get('total', 0)}")
        for bucket in series.get("by_day", []):
            marker = "▓" * min(int(bucket.get("count", 0)), 20)
            lines.append(f"  {bucket['date']}  {bucket['count']:>3}  {marker}")

    if data.get("created"):
        _render_series("Created", data["created"])
    if data.get("completed"):
        _render_series("Completed", data["completed"])

    return "\n".join(lines)


@mcp.tool()
async def get_task(task_id: str) -> str:
    """Get full details of a packet (task) including activity log.

    Args:
        task_id: UUID of the task
    """
    data = await _get(f"/tasks/{task_id}")
    lines = [
        f"# {data['title']}",
        f"Status: {data['workflow_status']} | Priority: {data['priority']}",
    ]
    if data.get("assignee_name"):
        lines.append(f"Assignee: {data['assignee_name']}")
    if data.get("sub_project_id"):
        lines.append(f"Op: {data.get('sub_project_id')}")
    if data.get("is_blocked"):
        lines.append(f"BLOCKED: {data.get('blocked_reason', '')}")
    if data.get("estimate"):
        lines.append(f"Estimate: {data['estimate'].upper()} (~{data.get('estimate_hours', '?')}h)")
    if data.get("scope_flag"):
        lines.append(f"Scope: {data['scope_flag']}")
    if data.get("description"):
        lines.append(f"\n{data['description']}")
    if data.get("acceptance_criteria"):
        lines.append("\nAcceptance Criteria:")
        for c in data["acceptance_criteria"]:
            if isinstance(c, dict):
                check = "x" if c.get("checked") else " "
                extra = ""
                if c.get("delegated_to_name"):
                    status = c.get("linked_task_status", "pending")
                    extra = f" → {c['delegated_to_name']} [{status}]"
                lines.append(f"  [{check}] {c.get('text', '')}{extra}")
            else:
                lines.append(f"  [ ] {c}")
    if data.get("tags"):
        tags = ", ".join(f"{k}:{v}" for k, v in data["tags"].items())
        lines.append(f"\nTags: {tags}")
    if data.get("events"):
        lines.append("\nActivity:")
        for evt in data["events"][-10:]:
            meta = evt.get("metadata", {})
            if evt["event_type"] == "comment":
                lines.append(f"  [{evt.get('actor_name', '')}] {meta.get('body', '')}")
            elif evt["event_type"] == "status_change":
                lines.append(f"  {evt.get('actor_name', '')} moved {meta.get('from', '')} → {meta.get('to', '')}")
            else:
                lines.append(f"  {evt.get('actor_name', '')} {evt['event_type']}")
    return "\n".join(lines)


@mcp.tool()
async def get_summary(project_id: str) -> str:
    """Get a high-level project summary — Ops, progress, health.

    Args:
        project_id: UUID of the project
    """
    project = await _get(f"/projects/{project_id}")
    ops_data = await _get(f"/projects/{project_id}/sub-projects")
    ops = ops_data if isinstance(ops_data, list) else ops_data.get("items", ops_data)
    board = await _get(f"/projects/{project_id}/board")

    # Count tasks by status
    status_counts: dict[str, int] = {}
    total = 0
    for col in board.get("columns", []):
        name = col["state"]["name"]
        count = len(col["tasks"])
        if count:
            status_counts[name] = count
            total += count

    lines = [
        f"# {project['name']}",
        f"Status: {project['status']} | Progress: {project.get('progress_pct', 0)}%",
        f"Total packets: {total}",
        "",
        "## Pipeline:",
    ]
    for name, count in status_counts.items():
        lines.append(f"  {name}: {count}")

    lines.append("\n## Ops:")
    for op in ops:
        approved = " [APPROVED]" if op.get("approved_at") else ""
        lines.append(
            f"  - {op['name']} [{op['status']}] "
            f"{op.get('done_tasks', 0)}/{op.get('total_tasks', 0)} done{approved}"
        )

    return "\n".join(lines)


@mcp.tool()
async def get_progress(project_id: str) -> str:
    """Get client-safe project progress — per-Op summary with status breakdown.

    Args:
        project_id: UUID of the project
    """
    data = await _get(f"/projects/{project_id}/progress")
    lines = [
        f"# {data['project_name']}",
        f"Progress: {data['progress_pct']}% | Ops: {data['completed_ops']}/{data['total_ops']} complete",
    ]
    for op in data["ops"]:
        health = ""
        if op.get("health"):
            health = f" — {op['health']['label']}"
        blocked = f" ({op['blocked_count']} blocked)" if op.get("blocked_count") else ""
        lines.append(
            f"\n## {op['op_name']} [{op['status']}] {op['progress_pct']}%"
            f"{health}{blocked}"
        )
        lines.append(f"  {op['done_tasks']}/{op['total_tasks']} packets done")
        if op.get("by_status"):
            for status, count in op["by_status"].items():
                lines.append(f"  {status}: {count}")
    return "\n".join(lines)


@mcp.tool()
async def get_op_dashboard(op_id: str) -> str:
    """Op-level burndown / burnup dashboard — velocity, ETA, health, recent scope changes.

    Returns the Board's dashboard payload for one Op: total vs done tasks,
    the burnup series (weekly snapshots of scope + completed), velocity in
    packets/week, projected weeks remaining, scope added and completed in
    the last 7 days, and days since anything last moved.

    Args:
        op_id: UUID of the Op (sub-project)
    """
    d = await _get(f"/sub-projects/{op_id}/dashboard")
    lines = [
        f"# {d.get('op_name','?')}",
        f"Progress: {d.get('done_tasks',0)}/{d.get('total_tasks',0)} packets ({d.get('progress_pct',0)}%)",
    ]
    health = d.get("health") or {}
    if health:
        lines.append(f"Health:   {health.get('label','?')} — {health.get('detail','')}")
    vel = d.get("velocity_per_week")
    weeks_left = d.get("weeks_remaining")
    if vel is not None:
        eta = f", ETA ~{weeks_left} weeks" if weeks_left is not None else ""
        lines.append(f"Velocity: {vel} packets/week{eta}")
    lines.append(
        f"Last 7d:  +{d.get('scope_added_last_7d',0)} scope, "
        f"{d.get('completed_last_7d',0)} completed"
    )
    if d.get("days_since_last_move") is not None:
        lines.append(f"Idle:     {d['days_since_last_move']} days since last movement")
    burnup = d.get("burnup") or []
    if burnup:
        lines.append("\nBurnup (week → scope / done):")
        for pt in burnup:
            wk = pt.get("week") or pt.get("date") or "?"
            scope = pt.get("scope") if "scope" in pt else pt.get("total_tasks", "?")
            done = pt.get("done") if "done" in pt else pt.get("done_tasks", "?")
            lines.append(f"  {wk}  scope={scope}  done={done}")
    return "\n".join(lines)


@mcp.tool()
async def list_members(project_id: str) -> str:
    """List team members of a project — returns user IDs, names, and roles.

    Useful before assigning tasks to find valid user IDs.

    Args:
        project_id: UUID of the project
    """
    data = await _get(f"/projects/{project_id}/members")
    members = data if isinstance(data, list) else data.get("items", [])
    if not members:
        return "No members found for this project."

    lines = [f"Project members ({len(members)}):"]
    for m in members:
        remit = f" — {m['remit']}" if m.get("remit") else ""
        lines.append(f"  - {m['user_name']} [{m['role']}] (user_id: {m['user_id']}){remit}")
    return "\n".join(lines)


# ── Write Tools ──────────────────────────────────────────────


@mcp.tool()
async def create_task(
    project_id: str,
    title: str,
    description: str = "",
    priority: str = "medium",
    op_id: str = "",
    as_draft: bool = True,
    estimate: str = "",
) -> str:
    """Create a new packet (task) in a project.

    MCP tickets land in **draft** by default — this tool is called by
    AI assistants, and draft is the review-first state reserved for
    AI-authored tickets. Human users creating via the Dispatch UI get
    `triage` (board-go's new default). Pass `as_draft=False` only when
    you're deliberately mirroring a human-created ticket.

    When creating tasks, always include an estimate based on complexity:
      xs (~30min) — trivial config change, typo fix, one-liner
      s  (~2hrs)  — small bug fix, minor feature, simple endpoint
      m  (~4hrs)  — standard feature, half-day task
      l  (~8hrs)  — significant feature, full-day task
      xl (~16hrs) — large feature, major refactor, multi-day task

    Args:
        project_id: UUID of the project
        title: Packet title
        description: Packet description
        priority: low, medium, high, critical
        op_id: Optional Op (sub-project) ID to assign to
        as_draft: Default True — AI-created tickets land in draft awaiting
            human review. Pass False to force triage (rare).
        estimate: T-shirt size estimate: xs, s, m, l, xl — always set this based on task complexity
    """
    body: dict[str, Any] = {
        "title": title,
        "priority": priority,
    }
    if description:
        body["description"] = description
    if as_draft:
        body["workflow_status"] = "draft"
    if estimate:
        body["estimate"] = estimate

    if op_id:
        data = await _post(f"/sub-projects/{op_id}/tasks", body)
    else:
        data = await _post(f"/projects/{project_id}/tasks", body)

    return f"Created: {data['title']} (id: {data['id']}, status: {data['workflow_status']})"


@mcp.tool()
async def move_task(task_id: str, to_status: str) -> str:
    """Move a packet to a new workflow status. For common transitions prefer
    the dedicated tools: groom_and_ready, submit_for_review, complete_task.

    Args:
        task_id: UUID of the task
        to_status: Target status (triage, sprint, in_progress, review, done, etc.)
    """
    data = await _put(f"/tasks/{task_id}/move", {"to_status": to_status})
    return f"Moved '{data['title']}' to {data['workflow_status']}"


@mcp.tool()
async def groom_and_ready(
    task_id: str,
    description: str = "",
    acceptance_criteria: list[str] | None = None,
    priority: str = "",
    assignee: str = "",
    estimate: str = "",
) -> str:
    """Groom a packet and move it from triage to ready.

    Updates whichever fields are provided, then advances to ready.
    Small tasks don't need every field — just provide what makes sense.

    IMPORTANT: Always provide an estimate when grooming. Use t-shirt sizes:
      xs (~30min) — trivial config change, typo fix, one-liner
      s  (~2hrs)  — small bug fix, minor feature, simple endpoint
      m  (~4hrs)  — standard feature, half-day task
      l  (~8hrs)  — significant feature, full-day task
      xl (~16hrs) — large feature, major refactor, multi-day task

    Args:
        task_id: UUID of the task
        description: Detailed description of what needs doing
        acceptance_criteria: List of acceptance criteria strings (e.g. ["API returns 200", "Tests pass"])
        priority: low, medium, high, critical
        assignee: User ID to assign to
        estimate: T-shirt size estimate: xs, s, m, l, xl — always set this based on task complexity
    """
    body: dict[str, Any] = {}
    if description:
        body["description"] = description
    if acceptance_criteria:
        body["acceptance_criteria"] = [{"text": t, "checked": False} for t in acceptance_criteria]
    if priority:
        body["priority"] = priority
    if assignee:
        body["assignee"] = assignee
    if estimate:
        body["estimate"] = estimate
    if body:
        await _put(f"/tasks/{task_id}", body)
    data = await _put(f"/tasks/{task_id}/move", {"to_status": "ready"})
    parts = [f"Groomed and moved '{data['title']}' to ready"]
    if description:
        parts.append("description updated")
    if acceptance_criteria:
        parts.append(f"{len(acceptance_criteria)} criteria set")
    if assignee:
        parts.append(f"assigned to {data.get('assignee_name', assignee)}")
    return " — ".join(parts)


@mcp.tool()
async def submit_for_review(
    task_id: str,
    summary: str = "",
    commit_sha: str = "",
) -> str:
    """Move a packet to review, optionally logging work done and commit SHA.

    Args:
        task_id: UUID of the task
        summary: Brief description of work completed
        commit_sha: Git commit SHA (if this was a code task)
    """
    if summary or commit_sha:
        parts = []
        if summary:
            parts.append(summary)
        if commit_sha:
            parts.append(f"Commit: {commit_sha}")
        await _post(f"/tasks/{task_id}/comment", {"body": "\n".join(parts)})
    data = await _put(f"/tasks/{task_id}/move", {"to_status": "review"})
    return f"'{data['title']}' submitted for review"


@mcp.tool()
async def complete_task(
    task_id: str,
    summary: str = "",
    commit_sha: str = "",
) -> str:
    """Complete a packet — adds a closing comment and moves to done.

    Works for any task: code tasks can include a commit SHA,
    non-code tasks (e.g. 'book doctors appointment') just get a summary.
    If no summary is provided the task is simply moved to done.

    Args:
        task_id: UUID of the task
        summary: What was done / outcome
        commit_sha: Git commit SHA (optional, only for code tasks)
    """
    if summary or commit_sha:
        parts = []
        if summary:
            parts.append(summary)
        if commit_sha:
            parts.append(f"Commit: {commit_sha}")
        await _post(f"/tasks/{task_id}/comment", {"body": "\n".join(parts)})
    data = await _put(f"/tasks/{task_id}/move", {"to_status": "done"})
    return f"Completed '{data['title']}'"


@mcp.tool()
async def add_comment(task_id: str, body: str) -> str:
    """Add a comment to a packet's activity log.

    Args:
        task_id: UUID of the task
        body: Comment text
    """
    await _post(f"/tasks/{task_id}/comment", {"body": body})
    return f"Comment added to {task_id}"


@mcp.tool()
async def assign_to_op(task_id: str, op_id: str) -> str:
    """Assign a packet to an Op (sub-project).

    Args:
        task_id: UUID of the task
        op_id: UUID of the Op to assign to
    """
    data = await _put(f"/tasks/{task_id}/assign-op", {"sub_project_id": op_id})
    return f"Assigned '{data['title']}' to Op {op_id}"


@mcp.tool()
async def assign_task(task_id: str, user_id: str) -> str:
    """Assign a team member to a packet (task).

    Accepts a KC subject UUID, username, email, or full name — this
    tool resolves to the KC subject before writing. "ross" and the
    UUID both work; the ambiguity danger is multiple exact matches,
    which raise with the candidate list so you can pick.

    Args:
        task_id: UUID of the task
        user_id: UUID / username / email / full name of the user to assign
    """
    resolved = await _resolve_user_id(user_id)
    await _post(f"/tasks/{task_id}/assignees", {"user_id": resolved})
    task = await _get(f"/tasks/{task_id}")
    note = f" (resolved from '{user_id}')" if resolved != user_id else ""
    return f"Assigned user {resolved}{note} to '{task['title']}'"


@mcp.tool()
async def create_op(
    project_id: str,
    name: str,
    description: str = "",
    objective: str = "",
) -> str:
    """Create a new Op (deliverable) in a project.

    Args:
        project_id: UUID of the project
        name: Op name
        description: Optional description
        objective: Optional objective statement
    """
    body: dict[str, Any] = {"name": name}
    if description:
        body["description"] = description
    if objective:
        body["objective"] = objective
    data = await _post(f"/projects/{project_id}/sub-projects", body)
    return f"Created Op: {data['name']} (id: {data['id']})"


@mcp.tool()
async def get_op(op_id: str) -> str:
    """Get full details of an Op (sub-project) including repos, notes, and criteria.

    Args:
        op_id: UUID of the Op
    """
    data = await _get(f"/sub-projects/{op_id}")
    lines = [
        f"# {data['name']}",
        f"Status: {data['status']} | Progress: {data.get('progress_pct', 0)}% ({data.get('done_tasks', 0)}/{data.get('total_tasks', 0)})",
    ]
    if data.get("planned_start") or data.get("planned_end"):
        auto = f" ({data['auto_planned']})" if data.get("auto_planned") else ""
        lines.append(f"Dates: {data.get('planned_start', '?')} → {data.get('planned_end', '?')}{auto}")
    if data.get("depends_on_op_id"):
        lines.append(f"Depends on: {data['depends_on_op_id']}")
    if data.get("approved_at"):
        lines.append(f"Approved: baseline {data.get('baseline_task_count', 0)} packets")
    if data.get("description"):
        lines.append(f"\n## Description\n{data['description']}")
    if data.get("objective"):
        lines.append(f"\n## Objective\n{data['objective']}")
    if data.get("acceptance_criteria"):
        lines.append("\n## Acceptance Criteria")
        for c in data["acceptance_criteria"]:
            if isinstance(c, dict):
                check = "x" if c.get("checked") else " "
                extra = ""
                if c.get("delegated_to_name"):
                    status = c.get("linked_task_status", "pending")
                    extra = f" → {c['delegated_to_name']} [{status}]"
                lines.append(f"  [{check}] {c.get('text', '')}{extra}")
            else:
                lines.append(f"  [ ] {c}")
    if data.get("technical_notes"):
        lines.append(f"\n## Technical Notes\n{data['technical_notes']}")
    return "\n".join(lines)


@mcp.tool()
async def update_op(
    op_id: str,
    name: str = "",
    description: str = "",
    objective: str = "",
    technical_notes: str = "",
    planned_start: str = "",
    planned_end: str = "",
) -> str:
    """Update an Op's name, description, objective, technical notes, or dates.

    Args:
        op_id: UUID of the Op
        name: New name (leave empty to keep current)
        description: New description (leave empty to keep current)
        objective: New objective (leave empty to keep current)
        technical_notes: New technical notes (leave empty to keep current)
        planned_start: Start date in YYYY-MM-DD format (leave empty to keep current)
        planned_end: End date in YYYY-MM-DD format (leave empty to keep current)
    """
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if description:
        body["description"] = description
    if objective:
        body["objective"] = objective
    if technical_notes:
        body["technical_notes"] = technical_notes
    if planned_start:
        body["planned_start"] = planned_start
    if planned_end:
        body["planned_end"] = planned_end
    if not body:
        return "Nothing to update — provide at least one field."
    data = await _put(f"/sub-projects/{op_id}", body)
    parts = [f"Updated Op: {data['name']}"]
    if data.get("planned_start"):
        parts.append(f"Start: {data['planned_start']}")
    if data.get("planned_end"):
        parts.append(f"End: {data['planned_end']}")
    return " | ".join(parts)


# ── Local repo path tracking ────────────────────────────────

LOCAL_REPOS_FILE = os.path.expanduser("~/.dispatch/local_repos.json")


def _load_local_repos() -> dict[str, str]:
    try:
        with open(LOCAL_REPOS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_local_repos(data: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(LOCAL_REPOS_FILE), exist_ok=True)
    with open(LOCAL_REPOS_FILE, "w") as f:
        json.dump(data, f, indent=2)


@mcp.tool()
async def update_project(
    project_id: str,
    name: str = "",
    description: str = "",
    repos: list[dict[str, str]] | None = None,
) -> str:
    """Update a project's name, description, or repos.

    Args:
        project_id: UUID of the project
        name: New name (leave empty to keep current)
        description: New description (leave empty to keep current)
        repos: New repo list, each with url, name, purpose (omit to keep current)
    """
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if description:
        body["description"] = description
    if repos is not None:
        body["repos"] = repos
    if not body:
        return "Nothing to update — provide at least one field."
    data = await _put(f"/projects/{project_id}", body)
    return f"Updated project: {data['name']}"


@mcp.tool()
async def set_repo_local_path(project_id: str, repo_url: str, local_path: str) -> str:
    """Record where a repo is cloned locally for a project.

    This lets other tools know the local path for a given repo.

    Args:
        project_id: UUID of the project
        repo_url: The repo URL (must match one in the project's repos list)
        local_path: Absolute path to the local clone
    """
    repos = _load_local_repos()
    key = f"{project_id}:{repo_url}"
    repos[key] = local_path
    _save_local_repos(repos)
    return f"Saved: {repo_url} → {local_path}"


@mcp.tool()
async def approve_op(op_id: str) -> str:
    """Approve an Op — baselines scope, any new packets become possible scope creep.

    Args:
        op_id: UUID of the Op to approve
    """
    data = await _post(f"/sub-projects/{op_id}/approve")
    return (
        f"Approved '{data['name']}' — baseline: {data.get('baseline_task_count', 0)} packets"
    )


# ── Update Tools ─────────────────────────────────────────────


@mcp.tool()
async def update_task(
    task_id: str,
    title: str = "",
    description: str = "",
    priority: str = "",
    estimate: str = "",
) -> str:
    """Update a packet's title, description, priority, or estimate.

    If a task has no estimate, set one based on complexity:
      xs (~30min), s (~2hrs), m (~4hrs), l (~8hrs), xl (~16hrs)

    Args:
        task_id: UUID of the task
        title: New title (leave empty to keep current)
        description: New description (leave empty to keep current)
        priority: New priority: low, medium, high, critical (leave empty to keep current)
        estimate: T-shirt size estimate: xs, s, m, l, xl (leave empty to keep current)
    """
    body: dict[str, Any] = {}
    if title:
        body["title"] = title
    if description:
        body["description"] = description
    if priority:
        body["priority"] = priority
    if estimate:
        body["estimate"] = estimate
    if not body:
        return "Nothing to update — provide at least one field."
    data = await _put(f"/tasks/{task_id}", body)
    return f"Updated: {data['title']}"


@mcp.tool()
async def block_task(task_id: str, reason: str) -> str:
    """Block a packet with a reason.

    Args:
        task_id: UUID of the task
        reason: Why the task is blocked
    """
    data = await _put(f"/tasks/{task_id}/block", {"reason": reason})
    return f"Blocked '{data['title']}': {reason}"


@mcp.tool()
async def unblock_task(task_id: str) -> str:
    """Unblock a previously blocked packet.

    Args:
        task_id: UUID of the task
    """
    data = await _put(f"/tasks/{task_id}/unblock")
    return f"Unblocked '{data['title']}'"


@mcp.tool()
async def set_scope_flag(task_id: str, flag: str) -> str:
    """Set the scope flag on a packet (for approved Ops).

    Args:
        task_id: UUID of the task
        flag: scope_creep, not_scope_creep, or possible_scope_creep
    """
    data = await _put(f"/tasks/{task_id}/scope-flag", {"scope_flag": flag})
    return f"Set scope flag on '{data['title']}' to {flag}"


@mcp.tool()
async def merge_task(target_task_id: str, source_task_id: str) -> str:
    """Merge (eat) one packet into another — absorbs description, criteria, tags, activity.

    Args:
        target_task_id: UUID of the packet that absorbs
        source_task_id: UUID of the packet being eaten (will be closed)
    """
    data = await _post(f"/tasks/{target_task_id}/merge", {"source_task_id": source_task_id})
    return f"Merged into '{data['title']}' — source packet closed"


@mcp.tool()
async def set_tag(task_id: str, key: str, value: str) -> str:
    """Set a tag on a packet.

    Args:
        task_id: UUID of the task
        key: Tag key
        value: Tag value
    """
    data = await _put(f"/tasks/{task_id}/tags/{key}", {"value": value})
    return f"Tag set: {key}={value}"


@mcp.tool()
async def set_project_domains(project_id: str, domains: list[str]) -> str:
    """Set allowed email domains for auto-viewer access on a project.

    Args:
        project_id: UUID of the project
        domains: List of email domains (e.g. ["hertex.co.za", "client.com"])
    """
    data = await _put(f"/projects/{project_id}/domains", {"allowed_domains": domains})
    ds = data.get("allowed_domains", [])
    return f"Domains set on '{data['name']}': {', '.join(ds) if ds else '(none)'}"


@mcp.tool()
async def reject_draft(task_id: str, feedback: str) -> str:
    """Reject a draft packet with feedback — keeps it in draft for rework.

    Args:
        task_id: UUID of the task
        feedback: Feedback explaining why the draft was rejected
    """
    data = await _post(f"/tasks/{task_id}/reject-draft", {"feedback": feedback})
    return f"Rejected draft '{data['title']}' with feedback"


@mcp.tool()
async def approve_drafts(task_ids: list[str]) -> str:
    """Bulk approve draft packets — moves them from draft to triage.

    project_id is inferred by board-go from the task rows (drafts scope
    to the caller's org via JWT), so no project id is needed.

    Args:
        task_ids: List of task UUIDs to approve
    """
    await _post("/tasks/approve-drafts", {"task_ids": task_ids})
    return f"Approved {len(task_ids)} drafts"


@mcp.tool()
async def finalise_op(op_id: str) -> str:
    """Finalise (deliver) an Op when all packets are done.

    Args:
        op_id: UUID of the Op to finalise
    """
    data = await _put(f"/sub-projects/{op_id}/finalise")
    return f"Finalised '{data['name']}' — status: {data['status']}"


@mcp.tool()
async def delegate_criterion(task_id: str, criterion_index: int, user_id: str) -> str:
    """Delegate an acceptance criterion on a packet to another user.

    Spawns a new packet assigned to that user with the criterion text.
    When the spawned packet reaches done, the criterion auto-checks.

    Args:
        task_id: UUID of the parent task
        criterion_index: Zero-based index of the criterion to delegate
        user_id: UUID / username / email / full name of the user to delegate to
    """
    # Resolve user_id to a KC subject before writing — same pattern
    # assign_task uses. See _resolve_user_id docstring for the rules.
    user_id = await _resolve_user_id(user_id)
    data = await _post(
        f"/tasks/{task_id}/criteria/{criterion_index}/delegate",
        {"user_id": user_id},
    )
    return f"Delegated to {data.get('assignee_name', user_id)} — spawned packet '{data['title']}' (id: {data['id']})"


@mcp.tool()
async def delegate_op_criterion(op_id: str, criterion_index: int, user_id: str) -> str:
    """Delegate an acceptance criterion on an Op to another user.

    Spawns a new packet assigned to that user with the criterion text.
    When the spawned packet reaches done, the criterion auto-checks.

    Args:
        op_id: UUID of the Op (sub-project)
        criterion_index: Zero-based index of the criterion to delegate
        user_id: UUID / username / email / full name of the user to delegate to
    """
    user_id = await _resolve_user_id(user_id)
    data = await _post(
        f"/sub-projects/{op_id}/criteria/{criterion_index}/delegate",
        {"user_id": user_id},
    )
    return f"Delegated to {data.get('assignee_name', user_id)} — spawned packet '{data['title']}' (id: {data['id']})"


@mcp.tool()
async def list_op_documents(op_id: str) -> str:
    """List documents attached to an Op (sub-project).

    Use this to see what briefs, specs, or contracts are attached to an Op
    before creating or grooming tasks.

    Args:
        op_id: UUID of the Op
    """
    data = await _get(f"/sub-projects/{op_id}/attachments")
    docs = data if isinstance(data, list) else []
    if not docs:
        return "No documents attached to this Op."
    lines = [f"{len(docs)} document(s):"]
    for d in docs:
        size = d.get("size_bytes", 0)
        size_str = f"{size // 1024}KB" if size and size >= 1024 else f"{size}B" if size else ""
        lines.append(f"  - {d['filename']} ({d.get('content_type', 'unknown')}, {size_str}) [id: {d['id']}]")
    return "\n".join(lines)


# ── AI Runner Tools ──────────────────────────────────────────


AI_DEFAULTS = {
    "enabled": True,
    "auto_classify": True,
    "auto_groom": True,
    "auto_bootstrap": True,
    "auto_suggest": True,
    "tone": "concise",
    "model_tier": "haiku",
    "classify_min_confidence": "medium",
    "groom_max_tokens": 500,
    "groom_max_criteria": 4,
    "enrich_min_confidence": 0.6,
    "monthly_budget_usd": 10.0,
}


@mcp.tool()
async def get_ai_settings(project_id: str) -> str:
    """Get AI Runner settings for a project — what's enabled, model tier, budget, etc.

    Args:
        project_id: UUID of the project
    """
    project = await _get(f"/projects/{project_id}")
    raw = project.get("ai_runner_settings") or {}
    settings = {**AI_DEFAULTS, **raw}

    lines = [f"# AI Runner Settings — {project['name']}"]
    on_off = {True: "on", False: "off"}
    lines.append(f"\nEnabled: **{on_off[settings['enabled']]}**")
    lines.append(f"\n## Features")
    lines.append(f"  auto_classify: {on_off[settings['auto_classify']]}")
    lines.append(f"  auto_groom: {on_off[settings['auto_groom']]}")
    lines.append(f"  auto_bootstrap: {on_off[settings['auto_bootstrap']]}")
    lines.append(f"  auto_suggest: {on_off[settings['auto_suggest']]}")
    lines.append(f"\n## Model")
    lines.append(f"  model_tier: {settings['model_tier']}")
    lines.append(f"  tone: {settings['tone']}")
    lines.append(f"\n## Quality")
    lines.append(f"  classify_min_confidence: {settings['classify_min_confidence']}")
    lines.append(f"  groom_max_tokens: {settings['groom_max_tokens']}")
    lines.append(f"  groom_max_criteria: {settings['groom_max_criteria']}")
    lines.append(f"  enrich_min_confidence: {settings['enrich_min_confidence']}")
    lines.append(f"\n## Budget")
    lines.append(f"  monthly_budget_usd: ${settings['monthly_budget_usd']:.2f}")
    return "\n".join(lines)


@mcp.tool()
async def update_ai_settings(
    project_id: str,
    enabled: bool | None = None,
    auto_classify: bool | None = None,
    auto_groom: bool | None = None,
    auto_bootstrap: bool | None = None,
    auto_suggest: bool | None = None,
    tone: str = "",
    model_tier: str = "",
    classify_min_confidence: str = "",
    groom_max_tokens: int | None = None,
    groom_max_criteria: int | None = None,
    enrich_min_confidence: float | None = None,
    monthly_budget_usd: float | None = None,
) -> str:
    """Update AI Runner settings for a project. Only provided fields are changed.

    Args:
        project_id: UUID of the project
        enabled: Master kill switch — set false to disable all AI processing
        auto_classify: Auto-classify new tickets into Ops
        auto_groom: Auto-generate description, criteria, estimate
        auto_bootstrap: Auto-scan repos and generate workspace context
        auto_suggest: Auto-suggest assignees after grooming
        tone: "concise" or "thorough" — controls AI output verbosity
        model_tier: "haiku" (fast/cheap) or "sonnet" (smart/expensive)
        classify_min_confidence: "low", "medium", or "high" — minimum confidence to auto-assign Op
        groom_max_tokens: Max output tokens for groom responses (e.g. 500)
        groom_max_criteria: Max number of acceptance criteria to generate (e.g. 4)
        enrich_min_confidence: Minimum confidence (0.0-1.0) to use AI-enriched context
        monthly_budget_usd: Monthly spend cap in USD (e.g. 10.0)
    """
    # Fetch current settings, merge with updates
    project = await _get(f"/projects/{project_id}")
    current = {**AI_DEFAULTS, **(project.get("ai_runner_settings") or {})}

    if enabled is not None:
        current["enabled"] = enabled
    if auto_classify is not None:
        current["auto_classify"] = auto_classify
    if auto_groom is not None:
        current["auto_groom"] = auto_groom
    if auto_bootstrap is not None:
        current["auto_bootstrap"] = auto_bootstrap
    if auto_suggest is not None:
        current["auto_suggest"] = auto_suggest
    if tone:
        current["tone"] = tone
    if model_tier:
        current["model_tier"] = model_tier
    if classify_min_confidence:
        current["classify_min_confidence"] = classify_min_confidence
    if groom_max_tokens is not None:
        current["groom_max_tokens"] = groom_max_tokens
    if groom_max_criteria is not None:
        current["groom_max_criteria"] = groom_max_criteria
    if enrich_min_confidence is not None:
        current["enrich_min_confidence"] = enrich_min_confidence
    if monthly_budget_usd is not None:
        current["monthly_budget_usd"] = monthly_budget_usd

    await _put(f"/projects/{project_id}", {"ai_runner_settings": current})
    return f"AI Runner settings updated for '{project['name']}'"


@mcp.tool()
async def get_ai_usage(project_id: str, months: int = 3) -> str:
    """View AI Runner token spend and budget for a project.

    Shows monthly breakdown of API calls, tokens used, and cost by pipeline stage.

    Args:
        project_id: UUID of the project
        months: Number of months of history to show (default 3)
    """
    data = await _get(f"/projects/{project_id}/ai-usage", {"months": months})
    if not data:
        return "No AI usage recorded yet."

    # Get budget for context
    project = await _get(f"/projects/{project_id}")
    raw = project.get("ai_runner_settings") or {}
    budget = raw.get("monthly_budget_usd", AI_DEFAULTS["monthly_budget_usd"])

    lines = [f"# AI Usage — {project['name']}", f"Monthly budget: ${budget:.2f}\n"]
    for month in data:
        ym = month["year_month"]
        cost = month["total_cost_usd"]
        pct = (cost / budget * 100) if budget > 0 else 0
        lines.append(f"## {ym} — ${cost:.4f} ({pct:.0f}% of budget)")
        lines.append(f"  Calls: {month['total_calls']} | Tokens: {month['total_input_tokens']:,} in / {month['total_output_tokens']:,} out")
        if month.get("by_stage"):
            for s in month["by_stage"]:
                lines.append(f"    {s['stage']}: {s['call_count']} calls, ${s['cost_usd']:.4f}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def accept_suggestion(task_id: str) -> str:
    """Accept the AI Runner's assignee suggestion — assigns the suggested user to the task.

    Reads the ai_suggested_assignee tag, assigns that user, then clears suggestion tags.

    Args:
        task_id: UUID of the task
    """
    task = await _get(f"/tasks/{task_id}")
    tags = task.get("tags") or {}

    suggested_id = tags.get("ai_suggested_assignee")
    suggested_name = tags.get("ai_suggested_assignee_name", "unknown")
    if not suggested_id:
        return f"No AI suggestion on '{task['title']}' — nothing to accept."

    # Assign the suggested user
    await _post(f"/tasks/{task_id}/assignees", {"user_id": suggested_id})

    # Clear suggestion tags
    for tag_key in ["ai_suggested_assignee", "ai_suggested_assignee_name", "ai_suggestion_reason"]:
        if tag_key in tags:
            await _delete(f"/tasks/{task_id}/tags/{tag_key}")

    return f"Accepted: assigned {suggested_name} to '{task['title']}'"


@mcp.tool()
async def dismiss_suggestion(task_id: str) -> str:
    """Dismiss the AI Runner's assignee suggestion — clears suggestion tags without assigning.

    Args:
        task_id: UUID of the task
    """
    task = await _get(f"/tasks/{task_id}")
    tags = task.get("tags") or {}

    if not tags.get("ai_suggested_assignee"):
        return f"No AI suggestion on '{task['title']}' — nothing to dismiss."

    suggested_name = tags.get("ai_suggested_assignee_name", "unknown")
    for tag_key in ["ai_suggested_assignee", "ai_suggested_assignee_name", "ai_suggestion_reason"]:
        if tag_key in tags:
            await _delete(f"/tasks/{task_id}/tags/{tag_key}")

    return f"Dismissed suggestion of {suggested_name} on '{task['title']}'"


@mcp.tool()
async def list_suggestions(project_id: str) -> str:
    """List all pending AI assignee suggestions across a project.

    Shows tasks that have AI-suggested assignees waiting for acceptance.

    Args:
        project_id: UUID of the project
    """
    data = await _get(f"/projects/{project_id}/tasks", {"limit": 200})
    tasks = data.get("items", data) if isinstance(data, dict) else data

    suggestions = []
    for t in tasks:
        tags = t.get("tags") or {}
        if tags.get("ai_suggested_assignee"):
            suggestions.append({
                "task": t["title"],
                "task_id": t["id"],
                "status": t["workflow_status"],
                "suggested": tags.get("ai_suggested_assignee_name", "unknown"),
                "reason": tags.get("ai_suggestion_reason", ""),
            })

    if not suggestions:
        return "No pending AI suggestions."

    lines = [f"# Pending AI Suggestions ({len(suggestions)})"]
    for s in suggestions:
        lines.append(f"\n- **{s['task']}** [{s['status']}]")
        lines.append(f"  Suggested: {s['suggested']}")
        if s["reason"]:
            lines.append(f"  Reason: {s['reason']}")
        lines.append(f"  id: {s['task_id']}")
    return "\n".join(lines)


# ── AI Runner Trigger Tools ──────────────────────────────────
#
# Board-go emits webhook events per task/op/project mutation which
# AI Runner routes to the appropriate pipeline. The MCP tools below
# fire those events by hitting the relevant board-go endpoint —
# there's no direct /ai-trigger dispatcher (that was Python-era).


@mcp.tool()
async def trigger_reclassify(task_id: str) -> str:
    """Re-trigger AI classification on a task — re-assigns it to the correct Op.

    Use when AI classified a ticket into the wrong Op, or when the
    task title/description has changed significantly. Task must be in
    triage — board-go's trigger-classify only runs on that column.

    Args:
        task_id: UUID of the task to reclassify
    """
    await _post(f"/tasks/{task_id}/trigger-classify", {})
    return f"Triggered reclassify for task {task_id}"


@mcp.tool()
async def trigger_regroom(task_id: str) -> str:
    """Re-trigger AI grooming on a task — regenerates description, criteria, estimate.

    Use when requirements have changed and the AI-generated grooming
    is stale. Task must be in triage.

    Args:
        task_id: UUID of the task to re-groom
    """
    await _post(f"/tasks/{task_id}/trigger-groom", {})
    return f"Triggered regroom for task {task_id}"


@mcp.tool()
async def trigger_reenrich(op_id: str) -> str:
    """Re-trigger AI enrichment on an Op — regenerates summary, relevant
    files, confidence score.

    Does a no-op PATCH to the Op, which fires board-go's `op.updated`
    webhook. AI Runner's router maps that event to the ENRICH_OP
    pipeline. Restored in 9a27909f once board-go started emitting
    op.updated webhooks (previously the tool had nothing to trigger).

    Args:
        op_id: UUID of the Op to re-enrich
    """
    # board-go routes PATCH + PUT to the same handler for sub-projects
    # (see internal/api/router.go). _put covers both.
    await _put(f"/sub-projects/{op_id}", {})
    return f"Triggered re-enrich for Op {op_id}"


@mcp.tool()
async def trigger_rebootstrap(project_id: str) -> str:
    """Re-trigger LP bootstrap on a project — refreshes workspace
    context, re-fetches attached repos, rebuilds symbol index.

    Does a no-op PATCH to the project, which fires board-go's
    `project.updated` webhook (or `project.repos_changed` if the
    repos array changed). AI Runner's router maps either event to
    the REBOOTSTRAP pipeline. Restored in 9a27909f.

    Args:
        project_id: UUID of the project (LP) to rebootstrap
    """
    # Same PATCH/PUT alias on board-go — _put reaches the same handler
    # as a browser-side PATCH.
    await _put(f"/projects/{project_id}", {})
    return f"Triggered rebootstrap for project {project_id}"


# ── Notification Tools ───────────────────────────────────────
#
# First slice of a69b3c97. Lets Claude surface "you have N unread
# mentions" at the start of a session + bulk-clear from the CLI
# without opening the UI bell.


@mcp.tool()
async def list_notifications(unread_only: bool = False, limit: int = 20) -> str:
    """List notifications for the caller — mentions, task assignments, Op events.

    Use at the start of a standup or when the user asks "what's new /
    anything I missed". Pair with mark_all_notifications_read once
    they've been read into the chat.

    Args:
        unread_only: If true, only return unread notifications.
        limit: Max rows (default 20, max 200).
    """
    params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
    if unread_only:
        params["unread_only"] = "true"
    rows = await _get("/notifications", params)
    if not rows:
        return "No unread notifications." if unread_only else "No notifications."
    lines = []
    for n in rows:
        actor = n.get("actor_name") or n.get("actor_id") or "someone"
        body = n.get("body") or {}
        preview = body.get("preview") or body.get("comment") or body.get("title") or ""
        if len(preview) > 120:
            preview = preview[:120] + "…"
        read_marker = "" if n.get("read_at") else " [UNREAD]"
        task_ref = f" task={n['task_id']}" if n.get("task_id") else ""
        proj_ref = f" project={n['project_id']}" if n.get("project_id") else ""
        extra = f" — {preview}" if preview else ""
        lines.append(
            f"- {n['created_at'][:16]} [{n['kind']}]{read_marker} by {actor}"
            f"{task_ref}{proj_ref} (id: {n['id']}){extra}"
        )
    return "\n".join(lines)


@mcp.tool()
async def mark_notification_read(notification_id: str) -> str:
    """Mark one notification as read.

    Idempotent — a 204 (already read / not yours) returns the same
    confirmation as a fresh mark. Use mark_all_notifications_read for
    bulk clear instead of looping this.

    Args:
        notification_id: UUID of the notification row.
    """
    await _post(f"/notifications/{notification_id}/read", {})
    return f"Marked notification {notification_id} as read."


@mcp.tool()
async def mark_all_notifications_read() -> str:
    """Mark every notification for the caller as read.

    Clears the bell. Non-destructive — the rows stay, only `read_at`
    is stamped. Useful after a standup where the user has heard all
    the pending items.
    """
    await _post("/notifications/mark-all-read", {})
    return "Marked all notifications as read."


# ── My-tasks + inventory tools ───────────────────────────────
#
# Second slice of a69b3c97. The "what am I on / what's left to groom /
# who's on the team" basics that let Claude answer standup questions
# without the user naming a project first.


@mcp.tool()
async def get_my_tasks() -> str:
    """List every task assigned to the caller — the user's capacity stack.

    Includes planned + actual dates, status flags (late / overrunning),
    estimate badges. The right answer to "what am I working on today?"
    for the authenticated user.
    """
    rows = await _get("/tasks/mine")
    tasks = rows.get("items", rows) if isinstance(rows, dict) else rows
    if not tasks:
        return "No tasks assigned to you."
    lines = []
    for t in tasks:
        flags = []
        if t.get("is_blocked"):
            flags.append("BLOCKED")
        if t.get("is_late"):
            flags.append("LATE")
        if t.get("is_overrunning"):
            flags.append("OVERRUNNING")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        est = f" ({t['estimate']})" if t.get("estimate") else ""
        due = f" due={t['planned_end'][:10]}" if t.get("planned_end") else ""
        lines.append(
            f"- [{t['workflow_status']}] {t['title']}{est}{flag_str}{due} "
            f"(id: {t['id']})"
        )
    return "\n".join(lines)


@mcp.tool()
async def list_unscheduled(project_id: str = "") -> str:
    """List tasks not yet pulled into a sprint — the grooming queue.

    Optional project_id scopes the view; empty returns org-wide.
    Pairs with pick_next_task + groom_and_ready for a "clean the
    backlog" session.

    Args:
        project_id: Optional UUID — filter to one LP.
    """
    params: dict[str, Any] = {}
    if project_id:
        params["project_id"] = project_id
    rows = await _get("/tasks/unscheduled", params)
    tasks = rows.get("items", rows) if isinstance(rows, dict) else rows
    if not tasks:
        return "No unscheduled tasks." + (f" (project={project_id})" if project_id else "")
    lines = []
    for t in tasks:
        est = f" ({t['estimate']})" if t.get("estimate") else ""
        lines.append(
            f"- [{t['workflow_status']}] {t['title']}{est} "
            f"(priority: {t.get('priority', 'medium')}, id: {t['id']})"
        )
    return "\n".join(lines)


@mcp.tool()
async def list_users() -> str:
    """List users in the caller's org — assignee + mention candidates.

    Returns the slim shape board-go uses for assignee dropdowns (id,
    name, username, email). Scoped to the current org via /users (see
    board-go listUsersLightHandler) — gotBot members won't show up on
    the opscraft org and vice versa.
    """
    data = await _get("/users")
    users = data.get("users", []) if isinstance(data, dict) else data
    if not users:
        return "No users in this org."
    lines = []
    for u in users:
        name = u.get("name") or u.get("username") or u.get("email") or "unknown"
        email = f" <{u['email']}>" if u.get("email") else ""
        username = f" ({u['username']})" if u.get("username") and u.get("username") != name else ""
        lines.append(f"- {name}{username}{email} — id: {u['id']}")
    return "\n".join(lines)


# ── Workflow Intelligence Tools ──────────────────────────────


ESTIMATE_HOURS = {"xs": 0.5, "s": 2, "m": 4, "l": 8, "xl": 16}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@mcp.tool()
async def pick_next_task(project_id: str, user_id: str = "") -> str:
    """Recommend the best task to work on next — ranked by priority, urgency, and readiness.

    Analyses all unblocked tasks in ready/sprint states and scores them based on
    priority, due date proximity, estimate size, and whether they're assigned.

    Args:
        project_id: UUID of the project
        user_id: Optional UUID / username / email of the user to filter
            tasks assigned to. Resolves via list_members() if bare.
    """
    if user_id:
        user_id = await _resolve_user_id(user_id)
    board = await _get(f"/projects/{project_id}/board")
    project_name = board["project"]["name"]

    # Build Op name lookup
    ops = await _get(f"/projects/{project_id}/sub-projects")
    op_names = {str(op["id"]): op["name"] for op in (ops if isinstance(ops, list) else ops.get("items", []))}

    # Collect candidates: tasks in actionable states that aren't blocked
    actionable = {"ready", "sprint", "in_progress"}
    candidates = []
    for col in board["columns"]:
        slug = col["state"]["slug"]
        if slug not in actionable:
            continue
        for t in col["tasks"]:
            if t.get("is_blocked"):
                continue
            if user_id and t.get("assignee") and t["assignee"] != user_id:
                continue
            candidates.append({**t, "_status": slug})

    if not candidates:
        return f"No actionable tasks found in {project_name}. Board is clear!"

    # Score: lower = do first
    today = _dt.date.today()
    scored = []
    for t in candidates:
        score = PRIORITY_RANK.get(t.get("priority", "medium"), 2) * 10

        status = t["_status"]
        if status == "in_progress":
            score -= 20
        elif status == "sprint":
            score -= 5

        due = t.get("due_date")
        if due:
            due_date = _dt.date.fromisoformat(due) if isinstance(due, str) else due
            days_until = (due_date - today).days
            if days_until < 0:
                score -= 50
            elif days_until <= 3:
                score -= 30
            elif days_until <= 7:
                score -= 10

        if not t.get("estimate"):
            score += 5

        scored.append((score, t))

    scored.sort(key=lambda x: x[0])

    top = scored[:5]
    lines = [f"# Next Task Recommendations — {project_name}"]
    if user_id:
        lines[0] += f" (for {user_id})"
    lines.append("")

    for i, (score, t) in enumerate(top, 1):
        status = t["_status"]
        pri = t.get("priority", "medium")
        est = t.get("estimate", "?")
        assignee = t.get("assignee_name") or "unassigned"
        due = t.get("due_date")

        marker = ""
        if status == "in_progress":
            marker = " **[CONTINUE]**"
        elif due:
            due_date = _dt.date.fromisoformat(due) if isinstance(due, str) else due
            days = (due_date - today).days
            if days < 0:
                marker = f" **[OVERDUE by {-days}d]**"
            elif days <= 3:
                marker = f" **[DUE in {days}d]**"

        lines.append(f"{i}. **{t['title']}**{marker}")
        lines.append(f"   {pri} | {est} | {status} | {assignee}")
        if t.get("sub_project_id"):
            op_name = op_names.get(str(t["sub_project_id"]), str(t["sub_project_id"]))
            lines.append(f"   Op: {op_name}")
        lines.append(f"   id: {t['id']}")
        lines.append("")

    remaining = len(scored) - 5
    if remaining > 0:
        lines.append(f"_+ {remaining} more tasks in backlog_")

    return "\n".join(lines)


@mcp.tool()
async def standup_summary(project_id: str, user_id: str = "", days: int = 1) -> str:
    """Generate a daily standup summary — what happened, what's next, blockers.

    Fetches the board and recent task activity to produce a 3-section standup:
    done (completed recently), doing (in progress), and blocked.

    Args:
        project_id: UUID of the project
        user_id: Optional user ID to scope to one person's work
        days: How many days back to look (default 1, use 2 for Monday standups)
    """
    board = await _get(f"/projects/{project_id}/board")
    project_name = board["project"]["name"]

    in_progress = []
    in_review = []
    blocked = []
    recently_done = []

    for col in board["columns"]:
        slug = col["state"]["slug"]
        for t in col["tasks"]:
            if user_id and t.get("assignee") != user_id:
                continue
            if slug == "in_progress":
                if t.get("is_blocked"):
                    blocked.append(t)
                else:
                    in_progress.append(t)
            elif slug == "review":
                in_review.append(t)
            elif slug in ("done", "archived"):
                recently_done.append(t)

    confirmed_done = []
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    for t in recently_done[:20]:
        try:
            detail = await _get(f"/tasks/{t['id']}")
            events = detail.get("events", [])
            for e in reversed(events):
                if e.get("event_type") == "status_change":
                    meta = e.get("metadata", {})
                    if meta.get("to") in ("done", "archived"):
                        if e.get("created_at", "") >= cutoff_str:
                            confirmed_done.append(t)
                        break
        except Exception:
            pass

    up_next = []
    for col in board["columns"]:
        if col["state"]["slug"] == "sprint":
            for t in col["tasks"]:
                if user_id and t.get("assignee") != user_id:
                    continue
                if not t.get("is_blocked"):
                    up_next.append(t)

    period = "yesterday" if days == 1 else f"last {days} days"
    lines = [f"# Standup — {project_name}"]
    if user_id:
        name = user_id
        for col in board["columns"]:
            for t in col["tasks"]:
                if t.get("assignee") == user_id and t.get("assignee_name"):
                    name = t["assignee_name"]
                    break
        lines[0] += f" ({name})"
    lines.append("")

    lines.append(f"## Done ({period})")
    if confirmed_done:
        for t in confirmed_done:
            est = f" [{t.get('estimate', '?')}]" if t.get("estimate") else ""
            lines.append(f"  - {t['title']}{est}")
    else:
        lines.append("  _Nothing completed_")
    lines.append("")

    lines.append("## In Progress")
    if in_progress or in_review:
        for t in in_progress:
            est = f" [{t.get('estimate', '?')}]" if t.get("estimate") else ""
            lines.append(f"  - {t['title']}{est}")
        for t in in_review:
            est = f" [{t.get('estimate', '?')}]" if t.get("estimate") else ""
            lines.append(f"  - {t['title']}{est} _(in review)_")
    else:
        lines.append("  _Nothing in progress_")
    lines.append("")

    lines.append("## Blockers")
    if blocked:
        for t in blocked:
            reason = t.get("blocked_reason") or "no reason given"
            lines.append(f"  - **{t['title']}** — {reason}")
    else:
        lines.append("  _No blockers_")
    lines.append("")

    if up_next:
        lines.append("## Up Next (sprint)")
        for t in up_next[:3]:
            pri = t.get("priority", "medium")
            est = f" [{t.get('estimate', '?')}]" if t.get("estimate") else ""
            lines.append(f"  - {t['title']} ({pri}){est}")

    return "\n".join(lines)


@mcp.tool()
async def sprint_review(project_id: str) -> str:
    """Generate a sprint review — throughput, scope changes, health, and recommendations.

    Pulls the full project summary with metrics, alerts, and Op health to produce
    a comprehensive review suitable for team retrospectives or client updates.

    Args:
        project_id: UUID of the project
    """
    summary = await _get(f"/projects/{project_id}/summary")
    project = summary["project"]
    stats = summary["stats"]
    alerts = summary["alerts"]
    sprint = summary["sprint_capacity"]
    breakdown = summary.get("workflow_breakdown", {})

    lines = [f"# Sprint Review — {project['name']}"]
    lines.append(f"_Generated: {summary['generated_at'][:10]}_\n")

    lines.append("## Throughput")
    lines.append(f"  Completed this week: **{stats['tasks_completed_this_week']}**")
    lines.append(f"  4-week avg: **{stats['throughput_per_week']}/week**")
    lines.append(f"  Avg cycle time: **{stats['avg_cycle_time_days']} days**")
    lines.append(f"  Overall progress: **{project['progress_pct']}%** ({stats['completed_tasks']}/{stats['total_tasks']} tasks)")
    lines.append("")

    lines.append("## Sprint Load")
    lines.append(f"  Tasks in flight: **{sprint['task_count']}** ({sprint['total_hours']}h estimated)")
    if sprint.get("by_assignee"):
        for name, hours in sprint["by_assignee"].items():
            lines.append(f"    {name}: {hours}h")
    lines.append("")

    lines.append("## Board State")
    for slug, count in breakdown.items():
        if count > 0:
            lines.append(f"  {slug}: {count}")
    if stats.get("draft_pending_approval"):
        lines.append(f"  drafts pending: {stats['draft_pending_approval']}")
    lines.append("")

    subs = summary.get("sub_projects", [])
    if subs:
        lines.append("## Ops Health")
        for sub in subs:
            status_icon = "done" if sub["status"] == "completed" else sub["status"]
            pct = sub.get("progress_pct", 0)
            total = sub.get("total_tasks", 0)
            done = sub.get("completed_tasks", 0)
            track = ""
            if sub.get("on_track") is not None:
                track = " — on track" if sub["on_track"] else " — **at risk**"
            lines.append(f"  - **{sub['name']}** [{status_icon}] {pct}% ({done}/{total}){track}")
        lines.append("")

    has_alerts = any(alerts.get(k) for k in alerts)
    if has_alerts:
        lines.append("## Alerts")
        if alerts.get("overdue"):
            lines.append(f"  **Overdue ({len(alerts['overdue'])}):**")
            for a in alerts["overdue"][:5]:
                lines.append(f"    - {a['title']} ({a['days_overdue']}d overdue)")
        if alerts.get("blocked"):
            lines.append(f"  **Stale ({len(alerts['blocked'])}):**")
            for a in alerts["blocked"][:5]:
                reason = f" — {a['blocked_reason']}" if a.get("blocked_reason") else ""
                lines.append(f"    - {a['title']} ({a['days_since_movement']}d no movement){reason}")
        if alerts.get("wip_violations"):
            lines.append(f"  **WIP violations ({len(alerts['wip_violations'])}):**")
            for a in alerts["wip_violations"]:
                hard = " (HARD)" if a["hard_limit"] else ""
                lines.append(f"    - {a['state_name']}: {a['current_count']}/{a['wip_limit']}{hard}")
        if alerts.get("unassigned_in_sprint"):
            lines.append(f"  **Unassigned in sprint ({len(alerts['unassigned_in_sprint'])}):**")
            for a in alerts["unassigned_in_sprint"][:5]:
                lines.append(f"    - {a['title']}")
        lines.append("")

    billing = summary.get("billing", {})
    if billing.get("total_billable_value"):
        lines.append("## Billing")
        lines.append(f"  Ops delivered: {billing['sub_projects_delivered']}")
        lines.append(f"  Accepted: {billing['sub_projects_accepted']}")
        lines.append(f"  Billing ready: {billing['sub_projects_billing_ready']} (${billing['value_billing_ready']:,.2f})")
        lines.append(f"  Billed: {billing['sub_projects_billed']} (${billing['value_billed']:,.2f})")
        lines.append(f"  Total billable: ${billing['total_billable_value']:,.2f}")
        if billing.get("unbilled_accepted"):
            lines.append("  **Unbilled but accepted:**")
            for u in billing["unbilled_accepted"]:
                amt = f"${u['agreed_amount']:,.2f}" if u.get("agreed_amount") else "TBD"
                lines.append(f"    - {u['name']} ({amt})")
        lines.append("")

    lines.append("## Recommendations")
    recs = []
    if alerts.get("blocked"):
        recs.append(f"Unblock {len(alerts['blocked'])} stale task(s) — some haven't moved in 5+ days")
    if alerts.get("unassigned_in_sprint"):
        recs.append(f"Assign {len(alerts['unassigned_in_sprint'])} unassigned sprint task(s)")
    if alerts.get("wip_violations"):
        recs.append("Reduce WIP — finish in-progress work before pulling new tasks")
    if stats.get("overdue_count"):
        recs.append(f"Review {stats['overdue_count']} overdue task(s) — reschedule or close")
    if stats.get("draft_pending_approval", 0) > 3:
        recs.append(f"Review {stats['draft_pending_approval']} pending drafts")
    if not recs:
        recs.append("Board looks healthy — keep shipping!")
    for r in recs:
        lines.append(f"  - {r}")

    return "\n".join(lines)


# ── Local workspace scanning ─────────────────────────────────


_LANG_MARKERS = {
    "requirements.txt": "Python", "setup.py": "Python", "pyproject.toml": "Python",
    "Pipfile": "Python", "package.json": "JavaScript/TypeScript", "tsconfig.json": "TypeScript",
    "go.mod": "Go", "Cargo.toml": "Rust", "Gemfile": "Ruby", "pom.xml": "Java",
    "build.gradle": "Java/Kotlin", "composer.json": "PHP", "mix.exs": "Elixir",
    "pubspec.yaml": "Dart/Flutter", "*.csproj": "C#/.NET",
}

_FRAMEWORK_MARKERS = {
    "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "express": "Express", "next": "Next.js", "nuxt": "Nuxt",
    "react": "React", "vue": "Vue", "angular": "Angular",
    "svelte": "Svelte", "astro": "Astro", "tailwindcss": "Tailwind CSS",
    "sqlalchemy": "SQLAlchemy", "prisma": "Prisma", "drizzle": "Drizzle",
    "alembic": "Alembic",
}


def _scan_local_repo(repo_path: Path) -> dict:
    """Scan a single local repo for tech stack, structure, remote URL."""
    result: dict[str, Any] = {
        "name": repo_path.name,
        "path": str(repo_path),
        "url": "",
        "languages": [],
        "frameworks": [],
        "has_tests": False,
        "has_ci": False,
        "has_dockerfile": False,
        "key_dirs": [],
        "purpose": "",
    }

    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            result["url"] = r.stdout.strip()
    except Exception:
        pass

    langs = set()
    frameworks = set()
    top_files = {f.name for f in repo_path.iterdir() if f.is_file()}
    top_dirs = {d.name for d in repo_path.iterdir() if d.is_dir() and not d.name.startswith(".")}

    for marker, lang in _LANG_MARKERS.items():
        if marker in top_files:
            langs.add(lang)

    pkg_json = repo_path / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            for dep_key, fw_name in _FRAMEWORK_MARKERS.items():
                if dep_key in all_deps:
                    frameworks.add(fw_name)
        except Exception:
            pass

    req_txt = repo_path / "requirements.txt"
    if req_txt.exists():
        try:
            reqs = req_txt.read_text().lower()
            for dep_key, fw_name in _FRAMEWORK_MARKERS.items():
                if dep_key in reqs:
                    frameworks.add(fw_name)
        except Exception:
            pass

    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text().lower()
            for dep_key, fw_name in _FRAMEWORK_MARKERS.items():
                if dep_key in content:
                    frameworks.add(fw_name)
        except Exception:
            pass

    result["has_tests"] = any(
        d in top_dirs for d in ("tests", "test", "__tests__", "spec")
    )
    result["has_ci"] = (repo_path / ".github" / "workflows").is_dir()
    result["has_dockerfile"] = "Dockerfile" in top_files or "dockerfile" in top_files

    skip = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".nuxt"}
    result["key_dirs"] = sorted(d for d in top_dirs if d not in skip)[:15]

    result["languages"] = sorted(langs)
    result["frameworks"] = sorted(frameworks)
    return result


@mcp.tool()
async def scan_local_workspace(folder_path: str) -> str:
    """Scan a local folder for git repos and map out tech stack, structure, and remote URLs.

    Point this at a folder containing your cloned repos (or a single repo).
    Returns a summary of everything found — repos, languages, frameworks,
    and a suggested project description. Review the output then use
    sync_workspace_to_lp() to upload to Dispatch.

    Args:
        folder_path: Absolute path to folder containing repos (e.g. /Users/you/projects/my-app)
    """
    root = Path(folder_path).expanduser().resolve()
    if not root.exists():
        return f"Error: path does not exist: {root}"
    if not root.is_dir():
        return f"Error: not a directory: {root}"

    repos = []

    if (root / ".git").is_dir():
        repos.append(_scan_local_repo(root))
    else:
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / ".git").is_dir():
                repos.append(_scan_local_repo(child))

    if not repos:
        return f"No git repos found in {root}. Make sure the folder contains cloned repositories."

    all_langs = sorted({l for r in repos for l in r["languages"]})
    all_frameworks = sorted({f for r in repos for f in r["frameworks"]})

    lines = [f"# Workspace Scan: {root.name}", f"Path: `{root}`", f"Repos found: **{len(repos)}**", ""]

    if all_langs:
        lines.append(f"**Languages:** {', '.join(all_langs)}")
    if all_frameworks:
        lines.append(f"**Frameworks:** {', '.join(all_frameworks)}")
    lines.append("")

    lines.append("## Repos")
    for r in repos:
        url_line = f"  URL: `{r['url']}`" if r["url"] else "  URL: _(no remote)_"
        lines.append(f"### {r['name']}")
        lines.append(url_line)
        lines.append(f"  Path: `{r['path']}`")
        if r["languages"]:
            lines.append(f"  Languages: {', '.join(r['languages'])}")
        if r["frameworks"]:
            lines.append(f"  Frameworks: {', '.join(r['frameworks'])}")
        features = []
        if r["has_tests"]:
            features.append("tests")
        if r["has_ci"]:
            features.append("CI/CD")
        if r["has_dockerfile"]:
            features.append("Docker")
        if features:
            lines.append(f"  Features: {', '.join(features)}")
        if r["key_dirs"]:
            lines.append(f"  Key dirs: {', '.join(r['key_dirs'][:10])}")
        lines.append("")

    if len(repos) == 1:
        r = repos[0]
        desc_parts = []
        if r["frameworks"]:
            desc_parts.append(f"{' + '.join(r['frameworks'])} application")
        elif r["languages"]:
            desc_parts.append(f"{' / '.join(r['languages'])} project")
        else:
            desc_parts.append("Software project")
        suggested = f"{desc_parts[0]} — {r['name']}"
    else:
        desc_parts = []
        if all_frameworks:
            desc_parts.append(f"{' + '.join(all_frameworks[:4])}")
        elif all_langs:
            desc_parts.append(f"{' / '.join(all_langs)}")
        suggested = f"Multi-repo project ({len(repos)} repos): {', '.join(r['name'] for r in repos)}"
        if desc_parts:
            suggested += f". Stack: {desc_parts[0]}"

    lines.append("## Suggested Description")
    lines.append(f"> {suggested}")
    lines.append("")
    lines.append("## Next Steps")
    lines.append("Review the above. If it looks correct, run **sync_workspace_to_lp** with:")
    lines.append(f"- `folder_path`: `{root}`")
    lines.append("- `project_id`: the UUID of your Launch Pad")
    lines.append("- Optionally override the `description`")

    return "\n".join(lines)


@mcp.tool()
async def sync_workspace_to_lp(
    project_id: str,
    folder_path: str,
    description: str = "",
    trigger_bootstrap: bool = True,
) -> str:
    """Upload scanned local repos to a Dispatch Launch Pad and trigger AI bootstrap.

    Run scan_local_workspace() first to preview what will be uploaded.
    This tool:
    1. Scans the folder for git repos
    2. Updates the LP with repo URLs, names, and description
    3. Records local paths for each repo
    4. Optionally triggers AI bootstrap to generate SYSTEM_SPEC.md

    Args:
        project_id: UUID of the Launch Pad to update
        folder_path: Same path you used with scan_local_workspace
        description: Project description (leave empty to keep current)
        trigger_bootstrap: Whether to trigger AI bootstrap after syncing (default: true)
    """
    root = Path(folder_path).expanduser().resolve()
    if not root.exists():
        return f"Error: path does not exist: {root}"

    scan_results = []
    if (root / ".git").is_dir():
        scan_results.append(_scan_local_repo(root))
    else:
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / ".git").is_dir():
                scan_results.append(_scan_local_repo(child))

    if not scan_results:
        return f"No git repos found in {root}"

    repos_with_url = [r for r in scan_results if r["url"]]
    repos_without_url = [r for r in scan_results if not r["url"]]

    if not repos_with_url:
        return (
            "No repos with remote URLs found. All repos need a git remote (origin) "
            "to be uploaded to Dispatch. Run `git remote add origin <url>` first."
        )

    repo_refs = []
    for r in repos_with_url:
        purpose_parts = []
        if r["languages"]:
            purpose_parts.append(", ".join(r["languages"]))
        if r["frameworks"]:
            purpose_parts.append(", ".join(r["frameworks"]))
        repo_refs.append({
            "url": r["url"],
            "name": r["name"],
            "purpose": " — ".join(purpose_parts) if purpose_parts else "",
        })

    update_body: dict[str, Any] = {"repos": repo_refs}
    if description:
        update_body["description"] = description

    await _put(f"/projects/{project_id}", update_body)

    local_repos = _load_local_repos()
    for r in repos_with_url:
        key = f"{project_id}:{r['url']}"
        local_repos[key] = r["path"]
    _save_local_repos(local_repos)

    bootstrap_msg = ""
    if trigger_bootstrap:
        try:
            # AI Runner picks up project.updated events and routes them
            # to REBOOTSTRAP. The PUT above already updated the project
            # with the new repos list, which fires project.updated —
            # no separate trigger call needed.
            bootstrap_msg = "\nTriggered AI bootstrap — CLAUDE.md + SYSTEM_SPEC.md will be generated shortly."
        except Exception as e:
            bootstrap_msg = f"\nBootstrap trigger failed: {e}. You can trigger manually later."

    lines = [
        f"# Synced to Dispatch",
        f"Project: `{project_id}`",
        f"Repos uploaded: **{len(repo_refs)}**",
    ]
    for ref in repo_refs:
        lines.append(f"  - **{ref['name']}** — `{ref['url']}`")
        if ref["purpose"]:
            lines.append(f"    _{ref['purpose']}_")

    if repos_without_url:
        lines.append(f"\nSkipped {len(repos_without_url)} repo(s) without remote URL:")
        for r in repos_without_url:
            lines.append(f"  - {r['name']} (`{r['path']}`)")

    if description:
        lines.append(f"\nDescription updated: _{description}_")

    lines.append(bootstrap_msg)
    lines.append(f"\nLocal paths saved to `~/.dispatch/local_repos.json`")

    return "\n".join(lines)


# ── LP + Op consolidation tools ────────────────────────────────

@mcp.tool()
async def consolidate_lp(source_lp: str, target_lp: str) -> str:
    """Merge a source LP into a target LP. Source's Ops + tasks flow
    into target; source LP is deleted. Target's permissions, members,
    and repos stay authoritative — source is a content donor, not a
    peer.

    Op name collisions get renamed to '{name} ({source_lp_name})' so
    nothing silently merges. Cross-service state (Lattice entity_links,
    RAG chunks, symbol_defs) is NOT touched — re-register repos +
    re-bless docs against the target LP after this.

    Requires owner role on BOTH LPs.

    Args:
        source_lp: UUID of the LP to merge from (will be deleted).
        target_lp: UUID of the LP to merge into (survives).
    """
    if source_lp == target_lp:
        raise Exception("consolidate_lp: source and target must differ")
    headers = await _auth_headers()
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BOARD_URL}/projects/{source_lp}/consolidate-into/{target_lp}",
            headers=headers, timeout=60,
        )
    if r.status_code == 403:
        raise Exception("consolidate_lp: forbidden — owner role needed on both LPs")
    if r.status_code == 404:
        raise Exception(f"consolidate_lp: not found — {r.text[:200]}")
    if r.status_code == 422:
        raise Exception(f"consolidate_lp: invalid — {r.text[:200]}")
    if not r.is_success:
        raise Exception(f"consolidate_lp HTTP {r.status_code}: {r.text[:200]}")
    return json.dumps(r.json(), indent=2)


@mcp.tool()
async def consolidate_op(source_op: str, target_op: str) -> str:
    """Merge a source Op into a target Op within the same LP. Source's
    tasks flow into target; source Op is deleted.

    Both Ops MUST belong to the same LP (otherwise this would secretly
    move tasks across LPs — use consolidate_lp for that instead).

    Requires owner role on the parent LP.

    Args:
        source_op: UUID of the Op to merge from (will be deleted).
        target_op: UUID of the Op to merge into (survives).
    """
    if source_op == target_op:
        raise Exception("consolidate_op: source and target must differ")
    headers = await _auth_headers()
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BOARD_URL}/sub-projects/{source_op}/consolidate-into/{target_op}",
            headers=headers, timeout=60,
        )
    if r.status_code == 403:
        raise Exception("consolidate_op: forbidden — owner role on parent LP required")
    if r.status_code == 404:
        raise Exception(f"consolidate_op: not found — {r.text[:200]}")
    if r.status_code == 422:
        raise Exception(f"consolidate_op: invalid — {r.text[:200]}")
    if not r.is_success:
        raise Exception(f"consolidate_op HTTP {r.status_code}: {r.text[:200]}")
    return json.dumps(r.json(), indent=2)


from lattice_tools import register_lattice_tools
from chrono_tools import register_chrono_tools
from rag_tools import register_rag_tools
from workspace_tools import register_workspace_tools
from symbol_tools import register_symbol_tools
from auth import make_org_id_fn

# workspace + symbol tools need the caller's org_id from the JWT
# (path-scoped endpoints). Reuse the same auth flow via a lazy JWT
# decoder — no extra Keycloak call.
_org_id = make_org_id_fn(_auth_headers)

register_lattice_tools(mcp, _auth_headers)
register_chrono_tools(mcp, _auth_headers)
register_rag_tools(mcp, _auth_headers)
register_workspace_tools(mcp, _auth_headers, _org_id)
register_symbol_tools(mcp, _auth_headers, _org_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
