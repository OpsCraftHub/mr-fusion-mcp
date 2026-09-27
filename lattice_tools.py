"""Lattice document tools for Claude — create, read, update, link, search docs."""

import json
import os
from typing import Any

import httpx

LATTICE_URL = os.getenv("LATTICE_URL", "http://localhost:8007/api/v1")


async def _lattice_get(path: str, auth_headers: dict, params: dict | None = None) -> Any:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{LATTICE_URL}{path}", params=params, headers=auth_headers, timeout=30)
        if not r.is_success:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:200]
            raise Exception(f"Lattice {r.status_code}: {detail}")
        return r.json()


async def _lattice_post(path: str, auth_headers: dict, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json", **auth_headers}
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{LATTICE_URL}{path}", json=body or {}, headers=headers, timeout=30)
        if not r.is_success:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:200]
            raise Exception(f"Lattice {r.status_code}: {detail}")
        return r.json()


async def _lattice_put(path: str, auth_headers: dict, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json", **auth_headers}
    async with httpx.AsyncClient() as c:
        r = await c.put(f"{LATTICE_URL}{path}", json=body or {}, headers=headers, timeout=30)
        if not r.is_success:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:200]
            raise Exception(f"Lattice {r.status_code}: {detail}")
        return r.json()


async def _lattice_delete(path: str, auth_headers: dict) -> str:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{LATTICE_URL}{path}", headers=auth_headers, timeout=30)
        if not r.is_success:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:200]
            raise Exception(f"Lattice {r.status_code}: {detail}")
        return "ok"


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def register_lattice_tools(mcp, auth_headers_fn):
    """Register Lattice document tools on the MCP server."""

    @mcp.tool()
    async def create_document(
        title: str,
        content: str = "",
        doc_type: str = "note",
        folder_id: str = "",
        entity_type: str = "",
        entity_id: str = "",
        is_context: bool = False,
    ) -> str:
        """Create a Lattice document (markdown body). Optionally link to a Board entity.

        Args:
            title: Document title
            content: Markdown content
            doc_type: Type — one of the registered doc types. Current registry:
                - note      — free-form note (default)
                - diagram   — Excalidraw diagram (use create_diagram instead)
                - adr       — Architecture Decision Record
                - spec      — technical specification
                - guide     — how-to guide
                - runbook   — operational procedure
                - glossary  — domain terms / entities
                - context   — sprint container context bundle
                - meeting   — meeting notes / decisions / actions
                - post      — blog / publication content (Emitter)
                Unknown values return 422 (invalid_doc_type). Registry is queryable
                via GET /api/v1/doc-types on the Lattice service.
            folder_id: Optional — UUID of a Lattice folder to file the doc under.
                       Use list_folders() to discover folder UUIDs. Omit to leave unfiled.
            entity_type: Optional — link to op, task, or project
            entity_id: Optional — UUID of the entity to link to
            is_context: If true, Claude should auto-read this doc when working on the entity
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {"title": title, "content": content, "doc_type": doc_type}
        if folder_id:
            body["folder_id"] = folder_id
        doc = await _lattice_post("/admin/posts", headers, body)

        result = f"Created document: {doc['title']} (id: {doc['id']})"

        if entity_type and entity_id:
            link_body = {
                "document_id": doc["id"],
                "entity_type": entity_type,
                "entity_id": entity_id,
                "is_context": is_context,
            }
            await _lattice_post("/admin/links", headers, link_body)
            ctx = " (context)" if is_context else ""
            result += f"\nLinked to {entity_type}/{entity_id}{ctx}"

        return result

    @mcp.tool()
    async def create_diagram(
        title: str,
        scene: dict,
        folder_id: str = "",
        entity_type: str = "",
        entity_id: str = "",
        is_context: bool = False,
    ) -> str:
        """Create a Lattice diagram (Excalidraw scene). Optionally link to a Board entity.

        Server auto-sets doc_type='diagram' when content_format='excalidraw' and
        no explicit doc_type is provided, so the chip on the UI reads "Diagram".

        Args:
            title: Document title
            scene: Excalidraw scene object — {type, version, source, elements, appState, files}.
                   Elements is a list of Excalidraw shape/text/arrow dicts. Colours should be
                   light-mode values (Excalidraw inverts them under theme="dark").
            folder_id: Optional — UUID of a Lattice folder to file the diagram under.
            entity_type: Optional — link to op, task, or project
            entity_id: Optional — UUID of the entity to link to
            is_context: If true, agents should auto-read this diagram when working on the entity
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "title": title,
            "content": "",
            "content_format": "excalidraw",
            "content_json": scene,
        }
        if folder_id:
            body["folder_id"] = folder_id
        doc = await _lattice_post("/admin/posts", headers, body)

        result = f"Created diagram: {doc['title']} (id: {doc['id']}, doc_type: {doc.get('doc_type')})"

        if entity_type and entity_id:
            link_body = {
                "document_id": doc["id"],
                "entity_type": entity_type,
                "entity_id": entity_id,
                "is_context": is_context,
            }
            await _lattice_post("/admin/links", headers, link_body)
            ctx = " (context)" if is_context else ""
            result += f"\nLinked to {entity_type}/{entity_id}{ctx}"

        return result

    @mcp.tool()
    async def list_doc_types() -> str:
        """List the registered Lattice doc types with their labels and descriptions.

        Useful when you need to know which doc_type to pass to create_document,
        or to check whether a type you want to use is registered.
        """
        headers = await auth_headers_fn()
        data = await _lattice_get("/doc-types", headers)
        lines = [f"Default: {data.get('default', 'note')}", "Registered types:"]
        for entry in data.get("types", []):
            lines.append(f"  - {entry['name']:10s} {entry['label']:12s} — {entry['description']}")
        return "\n".join(lines)

    @mcp.tool()
    async def read_document(document_id: str) -> str:
        """Read a Lattice document's full content.

        Args:
            document_id: UUID of the document
        """
        headers = await auth_headers_fn()
        doc = await _lattice_get(f"/admin/posts/{document_id}", headers)
        content = doc.get("content", "")
        truncated = ""
        if len(content) > 5000:
            content = content[:5000]
            truncated = "\n\n--- Content truncated at 5000 chars ---"

        return f"# {doc['title']}\n\nType: {doc['doc_type']} | Status: {doc['status']}\n\n{content}{truncated}"

    @mcp.tool()
    async def update_document(
        document_id: str,
        title: str = "",
        content: str = "",
    ) -> str:
        """Update a Lattice document's title and/or content.

        Args:
            document_id: UUID of the document
            title: New title (leave empty to keep current)
            content: New markdown content (leave empty to keep current)
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if content:
            body["content"] = content
        if not body:
            return "Nothing to update — provide title or content."

        doc = await _lattice_put(f"/admin/posts/{document_id}", headers, body)
        return f"Updated document: {doc['title']} (id: {doc['id']})"

    @mcp.tool()
    async def link_document(
        document_id: str,
        entity_type: str,
        entity_id: str,
        is_context: bool = False,
    ) -> str:
        """Link a Lattice document to a Board entity (op, task, or project).

        Args:
            document_id: UUID of the document
            entity_type: op, task, or project
            entity_id: UUID of the Board entity
            is_context: If true, Claude should auto-read this doc when working on the entity
        """
        headers = await auth_headers_fn()
        body = {
            "document_id": document_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "is_context": is_context,
        }
        link = await _lattice_post("/admin/links", headers, body)
        ctx = " (context)" if is_context else ""
        return f"Linked document {document_id} to {entity_type}/{entity_id}{ctx} (link_id: {link['id']})"

    @mcp.tool()
    async def list_linked_documents(
        entity_type: str,
        entity_id: str,
    ) -> str:
        """List documents linked to a Board entity.

        Args:
            entity_type: op, task, or project
            entity_id: UUID of the entity
        """
        headers = await auth_headers_fn()
        data = await _lattice_get(f"/links/by-entity/{entity_type}/{entity_id}", headers)
        docs = data.get("documents", [])
        if not docs:
            return f"No documents linked to {entity_type}/{entity_id}"

        lines = [f"Documents linked to {entity_type}/{entity_id}:"]
        for d in docs:
            ctx = " [CONTEXT]" if d.get("is_context") else ""
            lines.append(f"  - [{d['doc_type']}] {d['title']}{ctx} (id: {d['id']})")
        return "\n".join(lines)

    @mcp.tool()
    async def search_documents(
        query: str = "",
        doc_type: str = "",
        limit: int = 20,
    ) -> str:
        """Search your Lattice documents by title.

        Args:
            query: Title search text
            doc_type: Filter by type — note, post, spec, guide
            limit: Max results (default 20)
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {"limit": limit}
        if query:
            params["search"] = query
        if doc_type:
            params["doc_type"] = doc_type

        docs = await _lattice_get("/admin/posts", headers, params)
        if not docs:
            return "No documents found."

        lines = [f"Found {len(docs)} document(s):"]
        for d in docs:
            lines.append(f"  - [{d['doc_type']}] {d['title']} ({d['status']}) — id: {d['id']}")
        return "\n".join(lines)

    # ── Folders ──────────────────────────────────────────────

    @mcp.tool()
    async def create_folder(name: str, parent_id: str = "") -> str:
        """Create a Lattice folder. Optionally nest under a parent folder.

        Args:
            name: Folder name (unique per parent, per tenant)
            parent_id: Optional — UUID of the parent folder; omit for a top-level folder
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {"name": name}
        if parent_id:
            body["parent_id"] = parent_id
        folder = await _lattice_post("/admin/folders", headers, body)
        parent_note = f" (under parent {parent_id})" if parent_id else " (top-level)"
        return f"Created folder: {folder['name']}{parent_note} — id: {folder['id']}"

    @mcp.tool()
    async def list_folders(parent_id: str = "") -> str:
        """List Lattice folders. By default returns top-level folders; pass
        parent_id to list children of a specific folder.

        Args:
            parent_id: Optional — UUID of a folder to list children of. Omit for top-level.
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if parent_id:
            params["parent_id"] = parent_id
        folders = await _lattice_get("/admin/folders", headers, params)
        if not folders:
            return "No folders." if not parent_id else "No child folders in that parent."
        lines = [f"Found {len(folders)} folder(s):"]
        for f in folders:
            lines.append(
                f"  - {f['name']} (docs: {f['document_count']}, files: {f['file_count']}, "
                f"children: {f['child_count']}) — id: {f['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def list_folder_contents(folder_id: str) -> str:
        """List everything inside a folder — child folders, documents, and files.

        Args:
            folder_id: UUID of the folder to inspect
        """
        headers = await auth_headers_fn()
        data = await _lattice_get(f"/admin/folders/{folder_id}/contents", headers)
        lines: list[str] = []
        for f in data.get("folders", []):
            lines.append(f"  📁 {f['name']} — id: {f['id']}")
        for d in data.get("documents", []):
            lines.append(f"  📄 [{d['doc_type']}] {d['title']} ({d['status']}) — id: {d['id']}")
        for f in data.get("files", []):
            lines.append(f"  📎 {f['filename']} ({f['content_type']}, {f['size_bytes']}B) — id: {f['id']}")
        if not lines:
            return "Folder is empty."
        return "Folder contents:\n" + "\n".join(lines)

    @mcp.tool()
    async def rename_folder(folder_id: str, name: str) -> str:
        """Rename a Lattice folder. The slug regenerates from the new name.

        Args:
            folder_id: UUID of the folder
            name: New name (must be unique within the parent)
        """
        headers = await auth_headers_fn()
        folder = await _lattice_put(f"/admin/folders/{folder_id}", headers, {"name": name})
        return f"Renamed folder to '{folder['name']}' (slug: {folder['slug']})"

    @mcp.tool()
    async def move_folder(folder_id: str, parent_id: str = "") -> str:
        """Move a folder to a new parent (or to the top level).

        Args:
            folder_id: UUID of the folder to move
            parent_id: UUID of the new parent folder; omit to move to top level
        """
        headers = await auth_headers_fn()
        # Explicit None sends null so the server unnests the folder to top level.
        body: dict[str, Any] = {"parent_id": parent_id if parent_id else None}
        folder = await _lattice_put(f"/admin/folders/{folder_id}", headers, body)
        target = f"under parent {parent_id}" if parent_id else "to top level"
        return f"Moved folder '{folder['name']}' {target}"

    @mcp.tool()
    async def delete_folder(folder_id: str) -> str:
        """Delete a Lattice folder. Empty folders only — Lattice refuses to
        delete a folder that still contains documents, files, or child folders.

        Args:
            folder_id: UUID of the folder to delete
        """
        headers = await auth_headers_fn()
        await _lattice_delete(f"/admin/folders/{folder_id}", headers)
        return f"Deleted folder {folder_id}"

    @mcp.tool()
    async def move_document(document_id: str, folder_id: str = "") -> str:
        """Move a document into a folder, or out of any folder (unfile it).

        Args:
            document_id: UUID of the document
            folder_id: UUID of the destination folder; omit to unfile the document
        """
        headers = await auth_headers_fn()
        # Send explicit None so Lattice clears the folder_id column when unfiling.
        body: dict[str, Any] = {"folder_id": folder_id if folder_id else None}
        doc = await _lattice_put(f"/admin/posts/{document_id}", headers, body)
        if folder_id:
            return f"Moved '{doc['title']}' into folder {folder_id}"
        return f"Unfiled '{doc['title']}' (removed from folder)"
