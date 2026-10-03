"""Vault (Ledger-Go) tools for Claude — bookkeeping, cashbook, invoices, bills.

Covers the whole /api/v1 surface: chart of accounts, contacts, cashbook (CSV
import + categorise + bank rules), invoices, credit notes, bills (+AI draft
approval), payments, journal, reports, periods, members.

Design notes:
- Accepts human-friendly account_code and contact_name (resolved to UUID) as
  well as raw UUIDs — MCP callers usually don't have UUIDs handy.
- CSV import takes a local file_path; tool reads bytes and POSTs multipart.
"""

import os
from pathlib import Path
from typing import Any

import httpx

LEDGER_URL = os.getenv("LEDGER_URL", "http://localhost:8011/api/v1")
# AI Runner base URL — used only by retry_ai_invoice_draft. Points at
# the AI Runner service, NOT Ledger. Falls back to the local dev port
# on 8002 (the runner's default).
AI_RUNNER_URL = os.getenv("AI_RUNNER_URL", "http://localhost:8002")


# ── HTTP helpers ──────────────────────────────────────────────


async def _get(path: str, auth_headers: dict, params: dict | None = None) -> Any:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{LEDGER_URL}{path}", params=params, headers=auth_headers, timeout=30)
        _raise(r)
        return r.json()


async def _post(path: str, auth_headers: dict, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json", **auth_headers}
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{LEDGER_URL}{path}", json=body or {}, headers=headers, timeout=60)
        _raise(r)
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()


async def _put(path: str, auth_headers: dict, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json", **auth_headers}
    async with httpx.AsyncClient() as c:
        r = await c.put(f"{LEDGER_URL}{path}", json=body or {}, headers=headers, timeout=30)
        _raise(r)
        return r.json()


async def _patch(path: str, auth_headers: dict, body: dict | None = None) -> Any:
    headers = {"Content-Type": "application/json", **auth_headers}
    async with httpx.AsyncClient() as c:
        r = await c.patch(f"{LEDGER_URL}{path}", json=body or {}, headers=headers, timeout=30)
        _raise(r)
        return r.json()


async def _delete(path: str, auth_headers: dict) -> str:
    async with httpx.AsyncClient() as c:
        r = await c.delete(f"{LEDGER_URL}{path}", headers=auth_headers, timeout=30)
        _raise(r)
        return "ok"


async def _post_multipart(path: str, auth_headers: dict, files: dict) -> Any:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{LEDGER_URL}{path}", files=files, headers=auth_headers, timeout=120)
        _raise(r)
        return r.json()


def _raise(r: httpx.Response) -> None:
    if r.is_success:
        return
    try:
        detail = r.json()
    except Exception:
        detail = r.text[:400]
    raise Exception(f"Ledger {r.status_code} {r.request.method} {r.url.path}: {detail}")


# ── Resolvers (code/name → UUID) ──────────────────────────────


async def _resolve_account_id(auth_headers: dict, account_id: str = "", account_code: str = "") -> str:
    if account_id:
        return account_id
    if not account_code:
        raise Exception("Provide account_id (UUID) or account_code (e.g. '5000')")
    accts = await _get("/accounts", auth_headers)
    for a in accts:
        if a.get("code") == account_code:
            return a["id"]
    raise Exception(f"No account with code '{account_code}'. Use list_accounts to see the chart.")


async def _resolve_contact_id(auth_headers: dict, contact_id: str = "", contact_name: str = "") -> str:
    if contact_id:
        return contact_id
    if not contact_name:
        raise Exception("Provide contact_id (UUID) or contact_name")
    rows = await _get("/contacts", auth_headers, {"search": contact_name})
    if not rows:
        raise Exception(f"No contact matching '{contact_name}'. Use list_contacts or create_contact.")
    exact = [r for r in rows if r["name"].lower() == contact_name.lower()]
    picks = exact or rows
    if len(picks) > 1:
        names = ", ".join(f"{r['name']} ({r['id']})" for r in picks[:5])
        raise Exception(f"Ambiguous contact_name '{contact_name}' — matches: {names}")
    return picks[0]["id"]


# ── Tool registration ────────────────────────────────────────


def register_ledger_tools(mcp, auth_headers_fn):
    """Register Ledger (Vault) tools on the MCP server."""

    # ── Chart of accounts ────────────────────────────────────

    @mcp.tool()
    async def list_accounts(account_type: str = "", include_archived: bool = False) -> str:
        """List chart of accounts.

        Args:
            account_type: Filter by asset|liability|equity|revenue|expense (empty = all)
            include_archived: Include archived accounts
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if account_type:
            params["account_type"] = account_type
        if include_archived:
            params["include_archived"] = "true"
        rows = await _get("/accounts", headers, params)
        if not rows:
            return "No accounts. Use seed_accounts to seed the SA-SME chart."
        lines = [f"{len(rows)} account(s):"]
        for a in sorted(rows, key=lambda x: x["code"]):
            sys = " [SYSTEM]" if a.get("is_system") else ""
            role = f" role={a['system_role']}" if a.get("system_role") else ""
            arch = " [ARCHIVED]" if a.get("archived_at") else ""
            lines.append(f"  {a['code']} {a['name']:35} ({a['account_type']}/{a['normal_balance']}){role}{sys}{arch}")
        return "\n".join(lines)

    @mcp.tool()
    async def create_account(
        code: str,
        name: str,
        account_type: str,
        normal_balance: str,
        sub_type: str = "",
        system_role: str = "",
        vat_applicable: bool = False,
        allow_negative_balance: bool = True,
        parent_account_code: str = "",
    ) -> str:
        """Create a new chart-of-accounts entry (admin only).

        Args:
            code: Account code (e.g. '5700')
            name: Account name (e.g. 'Travel & Subsistence')
            account_type: asset | liability | equity | revenue | expense
            normal_balance: debit | credit (expense/asset = debit, revenue/liability/equity = credit)
            sub_type: Optional sub-classification (e.g. 'current_asset', 'fixed_asset')
            system_role: Optional role tag: bank | accounts_receivable | accounts_payable | vat_input | vat_output | revenue | expenses
            vat_applicable: True if VAT applies to postings on this account
            allow_negative_balance: Whether balance can go negative
            parent_account_code: Optional parent account code (for hierarchical charts)
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "code": code, "name": name, "account_type": account_type,
            "normal_balance": normal_balance,
            "vat_applicable": vat_applicable,
            "allow_negative_balance": allow_negative_balance,
        }
        if sub_type:
            body["sub_type"] = sub_type
        if system_role:
            body["system_role"] = system_role
        if parent_account_code:
            body["parent_id"] = await _resolve_account_id(headers, account_code=parent_account_code)
        acct = await _post("/accounts", headers, body)
        return f"Created account {acct['code']} — {acct['name']} (id: {acct['id']})"

    @mcp.tool()
    async def update_account(
        account_code: str = "",
        account_id: str = "",
        code: str = "",
        name: str = "",
        sub_type: str = "",
        system_role: str = "",
        vat_applicable: bool | None = None,
        allow_negative_balance: bool | None = None,
    ) -> str:
        """Update an existing account. Provide account_code OR account_id. Leave fields empty to keep unchanged."""
        headers = await auth_headers_fn()
        aid = await _resolve_account_id(headers, account_id, account_code)
        body: dict[str, Any] = {}
        if code:
            body["code"] = code
        if name:
            body["name"] = name
        if sub_type:
            body["sub_type"] = sub_type
        if system_role:
            body["system_role"] = system_role
        if vat_applicable is not None:
            body["vat_applicable"] = vat_applicable
        if allow_negative_balance is not None:
            body["allow_negative_balance"] = allow_negative_balance
        if not body:
            return "Nothing to update — provide at least one field."
        a = await _put(f"/accounts/{aid}", headers, body)
        return f"Updated {a['code']} — {a['name']}"

    @mcp.tool()
    async def archive_account(account_code: str = "", account_id: str = "") -> str:
        """Archive an account (admin only). System accounts cannot be archived."""
        headers = await auth_headers_fn()
        aid = await _resolve_account_id(headers, account_id, account_code)
        await _post(f"/accounts/{aid}/archive", headers)
        return f"Archived account {account_code or aid}"

    @mcp.tool()
    async def seed_accounts() -> str:
        """Seed the SA-SME default chart of accounts. Skips codes that already exist."""
        headers = await auth_headers_fn()
        created = await _post("/accounts/seed", headers)
        if not created:
            return "No new accounts created — chart already seeded."
        lines = [f"Seeded {len(created)} account(s):"]
        for a in created:
            lines.append(f"  {a['code']} {a['name']}")
        return "\n".join(lines)

    # ── Contacts ──────────────────────────────────────────────

    @mcp.tool()
    async def list_contacts(contact_type: str = "", search: str = "", include_archived: bool = False) -> str:
        """List contacts (customers, suppliers, or both).

        Args:
            contact_type: customer | supplier | both (empty = all)
            search: Substring match against name/email
            include_archived: Include archived contacts
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if contact_type:
            params["contact_type"] = contact_type
        if search:
            params["search"] = search
        if include_archived:
            params["include_archived"] = "true"
        rows = await _get("/contacts", headers, params)
        if not rows:
            return "No contacts."
        lines = [f"{len(rows)} contact(s):"]
        for c in rows:
            email = f" <{c['email']}>" if c.get("email") else ""
            arch = " [ARCHIVED]" if c.get("archived_at") else ""
            lines.append(f"  [{c['contact_type']:8}] {c['name']}{email}{arch} — id: {c['id']}")
        return "\n".join(lines)

    @mcp.tool()
    async def create_contact(
        name: str,
        contact_type: str,
        email: str = "",
        phone: str = "",
        address: str = "",
        vat_number: str = "",
        notes: str = "",
        default_revenue_account_code: str = "",
        default_expense_account_code: str = "",
    ) -> str:
        """Create a customer / supplier / both contact.

        Args:
            name: Contact display name
            contact_type: customer | supplier | both
            email: Contact email
            phone: Contact phone
            address: Postal/physical address
            vat_number: VAT registration number
            notes: Free-form notes
            default_revenue_account_code: Default 4xxx revenue account code
            default_expense_account_code: Default 5xxx expense account code
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {"name": name, "contact_type": contact_type}
        for k, v in {"email": email, "phone": phone, "address": address,
                     "vat_number": vat_number, "notes": notes}.items():
            if v:
                body[k] = v
        if default_revenue_account_code:
            body["default_revenue_account_id"] = await _resolve_account_id(
                headers, account_code=default_revenue_account_code)
        if default_expense_account_code:
            body["default_expense_account_id"] = await _resolve_account_id(
                headers, account_code=default_expense_account_code)
        c = await _post("/contacts", headers, body)
        return f"Created {c['contact_type']}: {c['name']} (id: {c['id']})"

    @mcp.tool()
    async def get_contact(contact_id: str = "", contact_name: str = "") -> str:
        """Fetch a contact by ID or name."""
        headers = await auth_headers_fn()
        cid = await _resolve_contact_id(headers, contact_id, contact_name)
        c = await _get(f"/contacts/{cid}", headers)
        lines = [
            f"Contact: {c['name']} [{c['contact_type']}]",
            f"  id: {c['id']}",
            f"  email: {c.get('email') or '-'}",
            f"  phone: {c.get('phone') or '-'}",
            f"  vat_number: {c.get('vat_number') or '-'}",
            f"  address: {c.get('address') or '-'}",
        ]
        if c.get("notes"):
            lines.append(f"  notes: {c['notes']}")
        return "\n".join(lines)

    @mcp.tool()
    async def update_contact(
        contact_id: str = "",
        contact_name: str = "",
        name: str = "",
        email: str = "",
        phone: str = "",
        address: str = "",
        vat_number: str = "",
        notes: str = "",
        contact_type: str = "",
    ) -> str:
        """Update a contact. Empty fields are left unchanged."""
        headers = await auth_headers_fn()
        cid = await _resolve_contact_id(headers, contact_id, contact_name)
        body: dict[str, Any] = {}
        for k, v in {"name": name, "email": email, "phone": phone, "address": address,
                     "vat_number": vat_number, "notes": notes, "contact_type": contact_type}.items():
            if v:
                body[k] = v
        if not body:
            return "Nothing to update."
        c = await _put(f"/contacts/{cid}", headers, body)
        return f"Updated contact: {c['name']}"

    @mcp.tool()
    async def archive_contact(contact_id: str = "", contact_name: str = "") -> str:
        """Archive a contact (admin only)."""
        headers = await auth_headers_fn()
        cid = await _resolve_contact_id(headers, contact_id, contact_name)
        await _post(f"/contacts/{cid}/archive", headers)
        return f"Archived contact {contact_name or cid}"

    @mcp.tool()
    async def approve_contact_review(contact_id: str = "", contact_name: str = "") -> str:
        """Approve an AI-created contact that's waiting on human review.

        Flips review_state from ai_pending_review → active. Needed before
        the contact can be used on invoices/bills raised from the UI.
        """
        headers = await auth_headers_fn()
        cid = await _resolve_contact_id(headers, contact_id, contact_name)
        c = await _post(f"/contacts/{cid}/approve-review", headers)
        return f"Approved: {c['name']} (review_state={c.get('review_state','?')})"

    @mcp.tool()
    async def get_contact_statement(
        contact_id: str = "",
        contact_name: str = "",
        from_date: str = "",
        to_date: str = "",
        unmatched_only: bool = False,
    ) -> str:
        """Unified statement for a contact — bills (AP), invoices (AR), and
        payments in date order with running AP/AR balances.

        Args:
            contact_id / contact_name: Who to pull the statement for
            from_date / to_date: YYYY-MM-DD, both optional
            unmatched_only: Hide fully-matched rows (leaves outstanding only)
        """
        headers = await auth_headers_fn()
        cid = await _resolve_contact_id(headers, contact_id, contact_name)
        params: dict[str, Any] = {}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        if unmatched_only:
            params["unmatched_only"] = "true"
        r = await _get(f"/contacts/{cid}/statement", headers, params)
        lines = [
            f"Statement: {r['contact_name']} [{r['contact_type']}]",
            f"  period: {r.get('from_date') or '—'} → {r.get('to_date') or '—'}",
            f"  opening AP={r['opening_ap']}  AR={r['opening_ar']}",
            f"  closing AP={r['closing_ap']}  AR={r['closing_ar']}",
            f"  {len(r['rows'])} row(s):",
        ]
        for row in r["rows"]:
            ref = f" ref={row['reference']}" if row.get("reference") else ""
            lines.append(
                f"    {row['date']}  [{row['type']:7}] [{row['match_status']:9}]  total={row['total']}  "
                f"matched={row['matched']}  out={row['outstanding']}  AP={row['running_ap']}  AR={row['running_ar']}{ref}"
            )
        return "\n".join(lines)

    # ── Cashbook: bank txns + CSV import + categorise + rules ─

    @mcp.tool()
    async def import_bank_csv(file_path: str) -> str:
        """Upload a bank statement CSV (Capitec format supported).

        Runs bank-rule auto-matching after import — transactions whose
        description matches an existing rule are categorised automatically.

        Args:
            file_path: Absolute path to a .csv file on this machine
        """
        headers = await auth_headers_fn()
        p = Path(file_path).expanduser()
        if not p.exists():
            raise Exception(f"File not found: {p}")
        if p.suffix.lower() != ".csv":
            raise Exception("File must be a .csv")
        with p.open("rb") as fh:
            files = {"file": (p.name, fh.read(), "text/csv")}
        result = await _post_multipart("/bank-transactions/import-csv", headers, files)
        lines = [
            f"Imported {result['imported']} txn(s), skipped {result['skipped']} duplicate(s), "
            f"auto-matched {result['auto_matched']} via rules ({result['total_rows']} rows read).",
        ]
        if result.get("date_range"):
            lines.append(f"  Date range: {result['date_range']}")
        if result.get("opening_balance") is not None:
            lines.append(f"  Opening: {result['opening_balance']}  Closing: {result['closing_balance']}")
        return "\n".join(lines)

    @mcp.tool()
    async def list_bank_transactions(
        reconciled: str = "",
        uncategorised_only: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> str:
        """List bank transactions.

        Args:
            reconciled: 'true' | 'false' | '' (empty = all)
            uncategorised_only: Show only txns without an account_id (still needing a category)
            limit: Max rows (up to 1000)
            offset: Pagination offset
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if reconciled:
            params["reconciled"] = reconciled
        rows = await _get("/bank-transactions", headers, params)
        if uncategorised_only:
            rows = [t for t in rows if not t.get("account_id")]
        if not rows:
            return "No matching transactions."
        lines = [f"{len(rows)} txn(s):"]
        for t in rows:
            rec = "✓" if t.get("is_reconciled") else " "
            cat = t.get("alloc_type") or "—"
            auto = " (auto)" if t.get("auto_matched") else ""
            amt = t.get("amount", "?")
            lines.append(
                f"  [{rec}] {t['transaction_date']}  {amt:>10}  {cat:16}{auto}  {t['description'][:60]}  id: {t['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def get_bank_transaction(txn_id: str) -> str:
        """Fetch full detail of one bank transaction."""
        headers = await auth_headers_fn()
        t = await _get(f"/bank-transactions/{txn_id}", headers)
        lines = [
            f"Txn {t['id']}",
            f"  date: {t['transaction_date']}  amount: {t['amount']}  fees: {t.get('fees') or '0'}",
            f"  description: {t['description']}",
            f"  reference: {t.get('reference') or '-'}",
            f"  source: {t.get('source')}  reconciled: {t.get('is_reconciled')}  auto_matched: {t.get('auto_matched')}",
            f"  alloc_type: {t.get('alloc_type') or '-'}  account_id: {t.get('account_id') or '-'}",
            f"  running_balance: {t.get('running_balance') or '-'}",
        ]
        if t.get("notes"):
            lines.append(f"  notes: {t['notes']}")
        return "\n".join(lines)

    @mcp.tool()
    async def categorise_bank_transaction(
        txn_id: str,
        alloc_type: str,
        account_code: str = "",
        account_id: str = "",
        contact_name: str = "",
        contact_id: str = "",
        invoice_id: str = "",
        bill_id: str = "",
        claim_vat: bool = False,
        create_rule: bool = True,
        notes: str = "",
    ) -> str:
        """Categorise a bank transaction. Creates a bank_rule so future imports auto-label.

        alloc_type options:
          - expense          → money out to a 5xxx expense account
          - revenue          → money in to a 4xxx revenue account
          - debtor_payment   → money in matched to an outstanding customer invoice (needs contact_name; invoice_id optional)
          - creditor_payment → money out matched to an outstanding supplier bill (needs contact_name; bill_id optional)
          - other_in         → misc money in (loan, refund, transfer) — pick any 1xxx-5xxx
          - other_out        → misc money out (drawings, loan repayment, transfer) — pick any 1xxx-5xxx

        Args:
            txn_id: UUID of the bank transaction
            alloc_type: See list above
            account_code: Account to post against (for expense/revenue/other_in/other_out)
            account_id: Same but by UUID
            contact_name: Customer (debtor_payment) or supplier (creditor_payment) name
            contact_id: Same but by UUID
            invoice_id: Optional invoice to match (debtor_payment)
            bill_id: Optional bill to match (creditor_payment)
            claim_vat: Whether to split out input/output VAT (expense/revenue/other only)
            create_rule: Persist as a bank_rule so matching descriptions auto-categorise on next import
            notes: Free-form notes on the allocation
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "alloc_type": alloc_type,
            "claim_vat": claim_vat,
            "create_rule": create_rule,
        }
        if notes:
            body["notes"] = notes
        if alloc_type in ("debtor_payment", "creditor_payment"):
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
            if invoice_id:
                body["invoice_id"] = invoice_id
            if bill_id:
                body["bill_id"] = bill_id
        else:
            body["account_id"] = await _resolve_account_id(headers, account_id, account_code)
        t = await _post(f"/bank-transactions/{txn_id}/categorise", headers, body)
        rule_note = " + rule saved" if create_rule and alloc_type not in ("debtor_payment", "creditor_payment") else ""
        return f"Categorised as {alloc_type}{rule_note}. amount={t['amount']}  desc={t['description'][:60]}"

    @mcp.tool()
    async def reconcile_bank_transaction(txn_id: str) -> str:
        """Toggle a bank transaction's reconciled flag."""
        headers = await auth_headers_fn()
        t = await _patch(f"/bank-transactions/{txn_id}/reconcile", headers)
        return f"Reconciled: {t.get('is_reconciled')} — {t['description'][:60]}"

    @mcp.tool()
    async def delete_bank_transaction(txn_id: str) -> str:
        """Delete an unreconciled bank transaction. Reconciled txns are locked."""
        headers = await auth_headers_fn()
        await _delete(f"/bank-transactions/{txn_id}", headers)
        return f"Deleted txn {txn_id}"

    @mcp.tool()
    async def force_delete_bank_transaction(txn_id: str) -> str:
        """Force-delete a bank transaction and everything linked to it.

        Reverses any posted journal, drops the linked payment row (with
        cascade to allocations), refreshes affected invoice/bill status,
        then deletes the bank_txn. Used for cleaning up cross-format
        import duplicates. Owner-scoped and destructive — no confirm.
        """
        headers = await auth_headers_fn()
        result = await _post(f"/bank-transactions/{txn_id}/force-delete", headers, None)
        rev_ids = result.get("reversal_journal_ids") or []
        pay_id = result.get("deleted_payment_id")
        inv_ids = result.get("affected_invoice_ids") or []
        bill_ids = result.get("affected_bill_ids") or []
        parts = [f"Force-deleted bank_txn {txn_id}"]
        if pay_id:
            parts.append(f"payment {pay_id}")
        if rev_ids:
            parts.append(f"reversal journals: {', '.join(rev_ids)}")
        if inv_ids:
            parts.append(f"refreshed invoices: {', '.join(inv_ids)}")
        if bill_ids:
            parts.append(f"refreshed bills: {', '.join(bill_ids)}")
        return " · ".join(parts)

    @mcp.tool()
    async def list_bank_rules() -> str:
        """List learned bank-description rules (auto-categorisation patterns)."""
        headers = await auth_headers_fn()
        rows = await _get("/bank-rules", headers)
        if not rows:
            return "No bank rules yet — they get created when you categorise transactions."
        accts = {a["id"]: a for a in await _get("/accounts", headers)}
        lines = [f"{len(rows)} rule(s):"]
        for r in rows:
            a = accts.get(r["account_id"], {})
            code = a.get("code", "?")
            name = a.get("name", "?")
            vat = " +VAT" if r.get("claim_vat") else ""
            matches = r.get("match_count", 0)
            lines.append(
                f"  [{r['alloc_type']:16}] '{r['pattern']}' → {code} {name}{vat} (matched {matches}x)  id: {r['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def delete_bank_rule(rule_id: str) -> str:
        """Delete a bank rule (stops auto-categorisation of matching descriptions)."""
        headers = await auth_headers_fn()
        await _delete(f"/bank-rules/{rule_id}", headers)
        return f"Deleted rule {rule_id}"

    @mcp.tool()
    async def create_bank_transaction(
        transaction_date: str,
        description: str,
        amount: str,
        account_code: str = "",
        account_id: str = "",
        bank_account_number: str = "",
        reference: str = "",
        fees: str = "",
        running_balance: str = "",
    ) -> str:
        """Add a bank transaction manually (source=manual). Use import_bank_csv
        for statement uploads; this is for one-off entries that aren't on a CSV.

        Args:
            transaction_date: YYYY-MM-DD
            description: Narrative on the statement
            amount: Signed amount as string ('1500.00' = money in, '-250.00' = money out)
            account_code: 1xxx bank account code (resolved to UUID)
            account_id: Same but by UUID (overrides account_code)
            bank_account_number: Account number string (for grouping across CSVs)
            reference: Payment reference
            fees: Separate bank-fee component (same sign convention)
            running_balance: Account balance after this line (helps cross-format dedup)
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "transaction_date": transaction_date,
            "description": description,
            "amount": amount,
        }
        if account_code or account_id:
            body["account_id"] = await _resolve_account_id(headers, account_id, account_code)
        if bank_account_number:
            body["bank_account_number"] = bank_account_number
        if reference:
            body["reference"] = reference
        if fees:
            body["fees"] = fees
        if running_balance:
            body["running_balance"] = running_balance
        t = await _post("/bank-transactions", headers, body)
        return f"Created bank_txn {t['id']}  {t['transaction_date']}  {t['amount']}  {t['description'][:60]}"

    @mcp.tool()
    async def unreconcile_bank_transaction(txn_id: str) -> str:
        """Unreconcile a categorised bank transaction (standard flow only).

        Clears the account_id/alloc_type/VAT fields and reverses the posted
        journal. Rejects payment-flow (debtor/creditor_payment) categorisations
        — those must be unwound via force_delete_bank_transaction instead.
        """
        headers = await auth_headers_fn()
        t = await _post(f"/bank-transactions/{txn_id}/unreconcile", headers)
        return f"Unreconciled txn {t['id']}  — reconciled={t.get('is_reconciled')}  alloc_type={t.get('alloc_type') or '—'}"

    @mcp.tool()
    async def preview_bank_csv(file_path: str) -> str:
        """Parse a bank CSV + detect format + count duplicates WITHOUT writing anything.

        Lets you sanity-check an import before committing it. Returns row counts,
        detected format (capitec / fnb / etc.), opening/closing balances, and
        any warnings/errors the parser hit.

        Args:
            file_path: Absolute path to a .csv on this machine
        """
        p = Path(file_path).expanduser()
        if not p.exists():
            raise Exception(f"File not found: {p}")
        if p.suffix.lower() != ".csv":
            raise Exception("File must be a .csv")
        headers = await auth_headers_fn()
        with p.open("rb") as fh:
            files = {"file": (p.name, fh.read(), "text/csv")}
        r = await _post_multipart("/bank-transactions/csv-preview", headers, files)
        lines = [
            f"Preview: {r['total_rows']} row(s), {r['new_rows']} new, {r['duplicate_rows']} duplicate.",
            f"  format: {r.get('format_detected','?')}  account: {r.get('account_number') or '-'}",
        ]
        if r.get("opening_balance") is not None:
            lines.append(f"  balance: {r['opening_balance']} → {r['closing_balance']}")
        if r.get("min_date"):
            lines.append(f"  date range: {r['min_date']} → {r['max_date']}")
        for w in r.get("warnings", []) or []:
            lines.append(f"  WARN  {w}")
        for e in r.get("errors", []) or []:
            lines.append(f"  ERR   {e}")
        return "\n".join(lines)

    @mcp.tool()
    async def list_import_batches() -> str:
        """List bank CSV import batches (one per upload)."""
        headers = await auth_headers_fn()
        rows = await _get("/bank-transactions/batches", headers)
        if not rows:
            return "No import batches."
        lines = [f"{len(rows)} batch(es):"]
        for b in rows:
            lines.append(
                f"  {b.get('created_at','?')}  {b.get('filename','?'):40}  rows={b.get('row_count','?')}  format={b.get('format_detected','?')}  id: {b['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def delete_import_batch(batch_id: str) -> str:
        """Delete an import batch and all its unreconciled rows.

        Rejected if any row in the batch has been reconciled — those must be
        unreconciled first (or force_delete_bank_transaction'd one by one).
        """
        headers = await auth_headers_fn()
        await _delete(f"/bank-transactions/batches/{batch_id}", headers)
        return f"Deleted import batch {batch_id}"

    @mcp.tool()
    async def backfill_import_hashes() -> str:
        """Recompute import_hash for every bank transaction using the current
        (account_number + date + amount + running_balance) formula.

        Idempotent one-shot used after unifying the hash algorithm — rows with
        the old description-based hash get updated so re-importing the same
        statement no longer creates duplicates. Skips rows missing
        bank_account_number or running_balance (manual entries).
        """
        headers = await auth_headers_fn()
        r = await _post("/bank-transactions/backfill-import-hashes", headers)
        return f"Backfill complete — total={r.get('total')}  updated={r.get('updated')}  skipped={r.get('skipped')}"

    @mcp.tool()
    async def backfill_journals() -> str:
        """Retroactively post journals for standard-flow categorised bank txns
        that were never journalled (data from before the auto-post was wired).

        Owner-only. Skips any txn without an account_id/alloc_type and any
        payment-flow row (those post journals via create_payment instead).
        """
        headers = await auth_headers_fn()
        r = await _post("/bank-transactions/backfill-journals", headers)
        return (
            f"Backfill complete — total={r.get('total')}  posted={r.get('posted')}  "
            f"skipped={r.get('skipped')}  errored={r.get('errored')}"
        )

    # ── Invoices ──────────────────────────────────────────────

    @mcp.tool()
    async def list_invoices(status: str = "", contact_name: str = "", contact_id: str = "") -> str:
        """List invoices, optionally filtered by status or contact.

        Args:
            status: draft | sent | partial | paid | void
            contact_name: Filter by customer name
            contact_id: Filter by customer UUID
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if contact_name or contact_id:
            params["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        rows = await _get("/invoices", headers, params)
        if not rows:
            return "No invoices."
        lines = [f"{len(rows)} invoice(s):"]
        for i in rows:
            lines.append(
                f"  [{i['status']:7}] {i['invoice_number']}  {i['invoice_date']} → {i['due_date']}  total={i['total']}  id: {i['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def get_invoice(invoice_id: str) -> str:
        """Fetch an invoice with all line items."""
        headers = await auth_headers_fn()
        inv = await _get(f"/invoices/{invoice_id}", headers)
        lines = [
            f"Invoice {inv['invoice_number']}  [{inv['status']}]",
            f"  date: {inv['invoice_date']}  due: {inv['due_date']}",
            f"  contact_id: {inv.get('contact_id') or '-'}",
            f"  subtotal: {inv['subtotal']}  vat: {inv['vat_amount']}  total: {inv['total']}",
        ]
        for l in inv.get("lines", []):
            lines.append(f"    - {l['description']}  qty={l['quantity']}  unit={l['unit_price']}  vat={l['vat_type']}  line={l['line_total']}")
        if inv.get("notes"):
            lines.append(f"  notes: {inv['notes']}")
        return "\n".join(lines)

    @mcp.tool()
    async def create_invoice(
        contact_name: str,
        invoice_date: str,
        lines: list[dict],
        contact_id: str = "",
        due_date: str = "",
        notes: str = "",
        currency: str = "",
    ) -> str:
        """Create a draft invoice.

        Args:
            contact_name: Customer name (resolved to id)
            invoice_date: YYYY-MM-DD
            lines: List of {description, quantity, unit_price, vat_type, vat_rate?}. vat_type = standard|zero_rated|exempt|out_of_scope
            contact_id: Alternative to contact_name
            due_date: YYYY-MM-DD (default: invoice_date + 30 days)
            notes: Free-form notes
            currency: ISO code (default org currency)
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "invoice_date": invoice_date,
            "lines": lines,
            "contact_id": await _resolve_contact_id(headers, contact_id, contact_name),
        }
        if due_date:
            body["due_date"] = due_date
        if notes:
            body["notes"] = notes
        if currency:
            body["currency"] = currency
        inv = await _post("/invoices", headers, body)
        return f"Created draft invoice {inv['invoice_number']} total={inv['total']} (id: {inv['id']})"

    @mcp.tool()
    async def send_invoice(invoice_id: str) -> str:
        """Move a draft invoice to 'sent' status."""
        headers = await auth_headers_fn()
        inv = await _post(f"/invoices/{invoice_id}/send", headers)
        return f"Sent invoice {inv['invoice_number']}"

    @mcp.tool()
    async def update_invoice(
        invoice_id: str,
        contact_name: str = "",
        contact_id: str = "",
        invoice_date: str = "",
        due_date: str = "",
        notes: str = "",
        reference: str = "",
    ) -> str:
        """Update an invoice's header fields. Line items are NOT editable
        post-creation — reverse + recreate if you need to change them.

        Empty fields are left unchanged.
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        for k, v in {"invoice_date": invoice_date, "due_date": due_date,
                     "notes": notes, "reference": reference}.items():
            if v:
                body[k] = v
        if not body:
            return "Nothing to update — provide at least one field."
        inv = await _put(f"/invoices/{invoice_id}", headers, body)
        return f"Updated invoice {inv['invoice_number']}"

    # ── Credit notes ──────────────────────────────────────────

    @mcp.tool()
    async def list_credit_notes(status: str = "") -> str:
        """List credit notes."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        rows = await _get("/credit-notes", headers, params)
        if not rows:
            return "No credit notes."
        lines = [f"{len(rows)} credit note(s):"]
        for cn in rows:
            lines.append(f"  [{cn['status']:7}] {cn.get('credit_note_number', '?')}  {cn.get('note_date', '')}  total={cn.get('total')}  id: {cn['id']}")
        return "\n".join(lines)

    @mcp.tool()
    async def post_credit_note(credit_note_id: str) -> str:
        """Post a draft credit note (creates journal entry)."""
        headers = await auth_headers_fn()
        cn = await _post(f"/credit-notes/{credit_note_id}/post", headers)
        return f"Posted credit note {cn.get('credit_note_number', credit_note_id)}"

    @mcp.tool()
    async def create_credit_note(
        credit_date: str,
        lines: list[dict],
        invoice_id: str = "",
        contact_name: str = "",
        contact_id: str = "",
        reason: str = "",
    ) -> str:
        """Create a draft credit note. Attach to an invoice OR a contact.

        Posts DR Revenue / CR AR when posted via post_credit_note.

        Args:
            credit_date: YYYY-MM-DD
            lines: [{description, quantity, unit_price, vat_type, vat_rate?}] — ≥1
            invoice_id: Original invoice being credited (optional — use contact alone for standalone CNs)
            contact_name / contact_id: Customer (required if no invoice_id)
            reason: Free-form explanation printed on the CN
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {"credit_date": credit_date, "lines": lines}
        if invoice_id:
            body["invoice_id"] = invoice_id
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        if reason:
            body["reason"] = reason
        r = await _post("/credit-notes", headers, body)
        cn = r.get("credit_note", r)
        return f"Created draft credit note {cn.get('credit_note_number','?')} (id: {cn.get('id')})"

    @mcp.tool()
    async def get_credit_note(credit_note_id: str) -> str:
        """Fetch a credit note with all line items."""
        headers = await auth_headers_fn()
        r = await _get(f"/credit-notes/{credit_note_id}", headers)
        cn = r.get("credit_note", r)
        rows = r.get("lines") or cn.get("lines") or []
        lines = [
            f"Credit note {cn.get('credit_note_number','?')}  [{cn.get('status','?')}]",
            f"  date: {cn.get('credit_date')}  invoice_id: {cn.get('invoice_id') or '-'}  contact_id: {cn.get('contact_id') or '-'}",
            f"  subtotal: {cn.get('subtotal')}  vat: {cn.get('vat_amount')}  total: {cn.get('total')}",
        ]
        for l in rows:
            lines.append(f"    - {l['description']}  qty={l['quantity']}  unit={l['unit_price']}  vat={l.get('vat_type','?')}  line={l.get('line_total','?')}")
        if cn.get("reason"):
            lines.append(f"  reason: {cn['reason']}")
        return "\n".join(lines)

    # ── Bills ─────────────────────────────────────────────────

    @mcp.tool()
    async def list_bills(status: str = "", contact_name: str = "", contact_id: str = "") -> str:
        """List bills. Status = draft | ai_draft | received | partial | paid | void."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if contact_name or contact_id:
            params["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        rows = await _get("/bills", headers, params)
        if not rows:
            return "No bills."
        lines = [f"{len(rows)} bill(s):"]
        for b in rows:
            lines.append(
                f"  [{b['status']:9}] {b['bill_number']}  {b['bill_date']}  total={b['total']}  id: {b['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def get_bill(bill_id: str) -> str:
        """Fetch a bill with all line items."""
        headers = await auth_headers_fn()
        b = await _get(f"/bills/{bill_id}", headers)
        lines = [
            f"Bill {b['bill_number']}  [{b['status']}]",
            f"  date: {b['bill_date']}  due: {b['due_date']}",
            f"  contact_id: {b.get('contact_id') or '-'}",
            f"  subtotal: {b['subtotal']}  vat: {b['vat_amount']}  total: {b['total']}  vat_claimable: {b.get('vat_claimable')}",
        ]
        for l in b.get("lines", []):
            lines.append(f"    - {l['description']}  qty={l['quantity']}  unit={l['unit_price']}  vat={l['vat_type']}  acct={l.get('account_id') or '-'}  line={l['line_total']}")
        if b.get("notes"):
            lines.append(f"  notes: {b['notes']}")
        return "\n".join(lines)

    @mcp.tool()
    async def create_bill(
        contact_name: str,
        bill_number: str,
        bill_date: str,
        lines: list[dict],
        contact_id: str = "",
        due_date: str = "",
        vat_claimable: bool = True,
        notes: str = "",
        currency: str = "",
    ) -> str:
        """Create a draft bill.

        Args:
            contact_name: Supplier name
            bill_number: Vendor's bill/invoice number
            bill_date: YYYY-MM-DD
            lines: List of {description, quantity, unit_price, vat_type, vat_rate?, account_id?}
            contact_id: Alternative to contact_name
            due_date: YYYY-MM-DD (default bill_date + 30 days)
            vat_claimable: False for non-tax-deductible bills
            notes: Free-form notes
            currency: ISO code
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "bill_number": bill_number,
            "bill_date": bill_date,
            "lines": lines,
            "vat_claimable": vat_claimable,
            "contact_id": await _resolve_contact_id(headers, contact_id, contact_name),
        }
        if due_date:
            body["due_date"] = due_date
        if notes:
            body["notes"] = notes
        if currency:
            body["currency"] = currency
        b = await _post("/bills", headers, body)
        return f"Created draft bill {b['bill_number']} total={b['total']} (id: {b['id']})"

    @mcp.tool()
    async def receive_bill(bill_id: str) -> str:
        """Move a draft bill to 'received' status."""
        headers = await auth_headers_fn()
        b = await _post(f"/bills/{bill_id}/receive", headers)
        return f"Received bill {b['bill_number']}"

    @mcp.tool()
    async def approve_ai_draft_bill(bill_id: str) -> str:
        """Approve an AI-drafted bill (posts journal + moves to draft status)."""
        headers = await auth_headers_fn()
        b = await _post(f"/bills/{bill_id}/approve-ai-draft", headers)
        return f"Approved AI draft: bill {b['bill_number']} now {b['status']}"

    @mcp.tool()
    async def reject_ai_draft_bill(bill_id: str) -> str:
        """Reject (delete) an AI-drafted bill."""
        headers = await auth_headers_fn()
        await _delete(f"/bills/{bill_id}/ai-draft", headers)
        return f"Rejected + deleted AI draft {bill_id}"

    @mcp.tool()
    async def update_bill(
        bill_id: str,
        contact_name: str = "",
        contact_id: str = "",
        bill_number: str = "",
        bill_date: str = "",
        due_date: str = "",
        notes: str = "",
        vat_claimable: bool | None = None,
        currency: str = "",
        exchange_rate: str = "",
        lines: list[dict] | None = None,
    ) -> str:
        """Update a bill. Header-only when `lines` is omitted; full replace
        (delete + reinsert lines) when `lines` is provided — only legal while
        the bill is still in 'draft' status. The replace path reverses the
        existing journal and reposts a new one inside the same tx.
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        for k, v in {"bill_number": bill_number, "bill_date": bill_date,
                     "due_date": due_date, "notes": notes,
                     "currency": currency, "exchange_rate": exchange_rate}.items():
            if v:
                body[k] = v
        if vat_claimable is not None:
            body["vat_claimable"] = vat_claimable
        if lines is not None:
            body["lines"] = lines
        if not body:
            return "Nothing to update."
        r = await _put(f"/bills/{bill_id}", headers, body)
        b = r.get("bill", r)
        return f"Updated bill {b.get('bill_number','?')}  status={b.get('status','?')}  total={b.get('total','?')}"

    @mcp.tool()
    async def update_ai_draft_bill(
        bill_id: str,
        contact_name: str = "",
        contact_id: str = "",
        bill_number: str = "",
        bill_date: str = "",
        due_date: str = "",
        notes: str = "",
        vat_claimable: bool | None = None,
        currency: str = "",
        exchange_rate: str = "",
        lines: list[dict] | None = None,
    ) -> str:
        """Edit an AI-drafted bill in place before approval. Same shape as
        update_bill but hits the ai-draft-specific endpoint so the bill stays
        in 'ai_draft' status rather than moving to 'draft'.
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        for k, v in {"bill_number": bill_number, "bill_date": bill_date,
                     "due_date": due_date, "notes": notes,
                     "currency": currency, "exchange_rate": exchange_rate}.items():
            if v:
                body[k] = v
        if vat_claimable is not None:
            body["vat_claimable"] = vat_claimable
        if lines is not None:
            body["lines"] = lines
        if not body:
            return "Nothing to update."
        r = await _put(f"/bills/{bill_id}/ai-draft", headers, body)
        b = r.get("bill", r)
        return f"Updated AI draft {b.get('bill_number','?')}  total={b.get('total','?')}"

    @mcp.tool()
    async def void_bill(bill_id: str, reason: str = "") -> str:
        """Void a bill — reverses the posted journal and marks status='void'.

        For 'this bill should never have existed' cases (duplicate, wrong
        supplier, etc.). Rejected if any payment is already allocated — those
        must be unallocated first. Not legal from 'void' or 'paid' states.
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        r = await _post(f"/bills/{bill_id}/void", headers, body)
        b = r.get("bill", r)
        return f"Voided bill {b.get('bill_number', bill_id)}  reason='{reason or '-'}'"

    @mcp.tool()
    async def bill_audit(bill_id: str) -> str:
        """Full audit trail for one bill (newest first)."""
        headers = await auth_headers_fn()
        rows = await _get(f"/bills/{bill_id}/audit", headers)
        if not rows:
            return "No audit events."
        lines = [f"{len(rows)} event(s):"]
        for e in rows:
            lines.append(
                f"  {e.get('created_at','?')}  {e.get('event_name','?'):26}  actor={e.get('actor_user_id','?')}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def upload_bill_attachment(bill_id: str, file_path: str) -> str:
        """Upload a receipt / supporting doc to a bill (max 10MB).

        Args:
            bill_id: UUID of the bill
            file_path: Absolute path to the file on this machine
        """
        p = Path(file_path).expanduser()
        if not p.exists():
            raise Exception(f"File not found: {p}")
        if p.stat().st_size > 10 * 1024 * 1024:
            raise Exception("Attachment must be under 10MB.")
        mime = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".heic": "image/heic",
        }.get(p.suffix.lower(), "application/octet-stream")
        headers = await auth_headers_fn()
        with p.open("rb") as fh:
            files = {"file": (p.name, fh.read(), mime)}
        a = await _post_multipart(f"/bills/{bill_id}/attachments", headers, files)
        return f"Uploaded {p.name} → attachment {a.get('id')}"

    @mcp.tool()
    async def list_bill_attachments(bill_id: str) -> str:
        """List attachments on a bill."""
        headers = await auth_headers_fn()
        rows = await _get(f"/bills/{bill_id}/attachments", headers)
        if not rows:
            return "No attachments."
        lines = [f"{len(rows)} attachment(s):"]
        for a in rows:
            lines.append(f"  {a.get('filename','?'):40}  {a.get('content_type','?'):24}  {a.get('size_bytes','?')} B  id: {a['id']}")
        return "\n".join(lines)

    @mcp.tool()
    async def delete_bill_attachment(bill_id: str, attachment_id: str) -> str:
        """Delete a bill attachment (removes the DB row AND the blob)."""
        headers = await auth_headers_fn()
        await _delete(f"/bills/{bill_id}/attachments/{attachment_id}", headers)
        return f"Deleted attachment {attachment_id}"

    @mcp.tool()
    async def retry_ai_invoice_draft(bill_id: str) -> str:
        """Re-run the AI invoice pipeline against an existing ai_draft
        bill's source email. Use after fixing pipeline code — the
        original invoice email is re-fetched from Signal, extracted
        fresh, contact + line accounts re-resolved, and the draft's
        header + lines are replaced IN PLACE (no duplicates).

        Requires AI_RUNNER_URL env var pointing at the AI Runner service.
        """
        headers = await auth_headers_fn()
        # Fetch the bill via the authed endpoint to get message_id + org.
        bill = await _get(f"/bills/{bill_id}", headers)
        message_id = bill.get("source_email_message_id")
        if not message_id:
            raise Exception(
                f"Bill {bill_id} has no source_email_message_id — nothing to retry against"
            )
        org_id = bill.get("org_id")
        if not org_id:
            raise Exception(f"Bill {bill_id} missing org_id — cannot retry")
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                f"{AI_RUNNER_URL.rstrip('/')}/api/v1/watcher_invoice/retry",
                json={"bill_id": bill_id, "message_id": str(message_id), "org_id": str(org_id)},
                headers={"X-Internal-Service": "true"},
            )
            if not r.is_success:
                try:
                    detail = r.json()
                except Exception:
                    detail = r.text[:400]
                raise Exception(f"AI Runner {r.status_code}: {detail}")
            out = r.json()
        return f"Retry {out.get('status')}: {out.get('summary')}"

    # ── Payments ──────────────────────────────────────────────

    @mcp.tool()
    async def list_payments(direction: str = "", contact_name: str = "", contact_id: str = "") -> str:
        """List payments. direction = received | made."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if direction:
            params["direction"] = direction
        if contact_name or contact_id:
            params["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        rows = await _get("/payments", headers, params)
        if not rows:
            return "No payments."
        lines = [f"{len(rows)} payment(s):"]
        for p in rows:
            allocs = len(p.get("allocations", []))
            lines.append(f"  [{p['direction']:8}] {p['payment_date']}  {p['amount']:>10}  ref={p.get('reference') or '-'}  allocs={allocs}  id: {p['id']}")
        return "\n".join(lines)

    @mcp.tool()
    async def create_payment(
        direction: str,
        payment_date: str,
        amount: str,
        contact_name: str = "",
        contact_id: str = "",
        reference: str = "",
        allocations: list[dict] | None = None,
        idempotency_key: str = "",
    ) -> str:
        """Record a payment. direction = received (customer pays us) | made (we pay supplier).

        Args:
            direction: received | made
            payment_date: YYYY-MM-DD
            amount: Payment amount as string (e.g. '1500.00')
            contact_name: Customer/supplier name
            contact_id: Alternative to contact_name
            reference: Payment reference
            allocations: [{invoice_id|bill_id, amount}] — invoice for received, bill for made
            idempotency_key: Optional idempotency key for retry safety
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "direction": direction,
            "payment_date": payment_date,
            "amount": amount,
            "allocations": allocations or [],
        }
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        if reference:
            body["reference"] = reference
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        p = await _post("/payments", headers, body)
        return f"Created {p['direction']} payment {p['amount']} on {p['payment_date']} (id: {p['id']})"

    @mcp.tool()
    async def get_payment(payment_id: str) -> str:
        """Fetch a payment with all its allocations."""
        headers = await auth_headers_fn()
        r = await _get(f"/payments/{payment_id}", headers)
        p = r.get("payment", r)
        allocs = r.get("allocations") or p.get("allocations") or []
        lines = [
            f"Payment {p['id']}",
            f"  date: {p['payment_date']}  direction: {p['direction']}  amount: {p['amount']}",
            f"  contact_id: {p.get('contact_id') or '-'}  reference: {p.get('reference') or '-'}",
            f"  {len(allocs)} allocation(s):",
        ]
        for a in allocs:
            target = f"bill={a['bill_id']}" if a.get("bill_id") else f"invoice={a.get('invoice_id','?')}"
            fx = f"  fx_amount={a['foreign_amount']}" if a.get("foreign_amount") else ""
            lines.append(f"    {target}  amount={a['amount']}{fx}")
        return "\n".join(lines)

    @mcp.tool()
    async def allocate_payment(payment_id: str, allocations: list[dict]) -> str:
        """Allocate (or further allocate) a payment to bills/invoices.

        The payment's bank journal was posted at cashbook-categorise time;
        this endpoint only records which docs the money settles against and
        updates each doc's status. If a target doc is foreign-currency, pass
        `foreign_amount` alongside `amount` so FX corrections can post.

        Args:
            payment_id: UUID of the payment
            allocations: [{bill_id?, invoice_id?, amount, foreign_amount?}] — one target per row
        """
        headers = await auth_headers_fn()
        r = await _post(f"/payments/{payment_id}/allocate", headers,
                        {"allocations": allocations})
        p = r.get("payment", r)
        allocs = r.get("allocations") or []
        return f"Allocated {len(allocs)} line(s) against payment {p['id']} (amount={p['amount']})"

    # ── Journal ───────────────────────────────────────────────

    @mcp.tool()
    async def list_journal_entries(source: str = "", posted: str = "", limit: int = 50) -> str:
        """List journal entries.

        Args:
            source: manual | invoice | bill | payment | cashbook_payment | ...
            posted: 'true' | 'false' | '' (all)
            limit: Max entries
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {"limit": limit}
        if source:
            params["source"] = source
        if posted:
            params["posted"] = posted
        rows = await _get("/journal-entries", headers, params)
        if not rows:
            return "No journal entries."
        lines = [f"{len(rows)} entry/ies:"]
        for e in rows:
            posted_flag = "POSTED" if e.get("is_posted") else "draft "
            lines.append(f"  [{posted_flag}] {e['entry_date']}  {e['source']:14}  {e['description'][:60]}  id: {e['id']}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_journal_entry(entry_id: str) -> str:
        """Fetch a journal entry with all debit/credit lines."""
        headers = await auth_headers_fn()
        e = await _get(f"/journal-entries/{entry_id}", headers)
        accts = {a["id"]: a for a in await _get("/accounts", headers)}
        lines = [
            f"Entry {e.get('entry_number') or e['id']}  [{'posted' if e['is_posted'] else 'draft'}]",
            f"  date: {e['entry_date']}  source: {e['source']}  ref: {e.get('reference') or '-'}",
            f"  description: {e['description']}",
        ]
        for l in e.get("lines", []):
            a = accts.get(l["account_id"], {})
            lines.append(f"    {a.get('code','?')} {a.get('name','?'):30}  DR={l['debit_amount']:>10}  CR={l['credit_amount']:>10}")
        return "\n".join(lines)

    @mcp.tool()
    async def create_journal_entry(
        entry_date: str,
        description: str,
        lines: list[dict],
        source: str = "manual",
        reference: str = "",
        currency: str = "",
        idempotency_key: str = "",
    ) -> str:
        """Create a draft journal entry. Debits must equal credits.

        Args:
            entry_date: YYYY-MM-DD
            description: What this entry records
            lines: List of {account_id, debit_amount, credit_amount, description?}
            source: manual (default) — do NOT use reserved values invoice/bill/payment
            reference: External reference
            currency: ISO code
            idempotency_key: Retry-safe key
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "entry_date": entry_date, "description": description,
            "source": source, "lines": lines,
        }
        if reference:
            body["reference"] = reference
        if currency:
            body["currency"] = currency
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        e = await _post("/journal-entries", headers, body)
        return f"Created draft entry (id: {e['id']}). Call post_journal_entry to commit."

    @mcp.tool()
    async def post_journal_entry(entry_id: str) -> str:
        """Post a draft journal entry (locks it into the hash chain)."""
        headers = await auth_headers_fn()
        e = await _post(f"/journal-entries/{entry_id}/post", headers)
        return f"Posted entry {e['id']}"

    @mcp.tool()
    async def reverse_journal_entry(entry_id: str) -> str:
        """Reverse a posted journal entry (swaps debits/credits, marks original reversed)."""
        headers = await auth_headers_fn()
        rev = await _post(f"/journal-entries/{entry_id}/reverse", headers)
        return f"Reversed. Reversal entry id: {rev['id']}"

    @mcp.tool()
    async def delete_draft_journal(entry_id: str) -> str:
        """Delete an UNPOSTED draft journal entry. Posted entries must be reversed."""
        headers = await auth_headers_fn()
        await _delete(f"/journal-entries/{entry_id}", headers)
        return f"Deleted draft {entry_id}"

    @mcp.tool()
    async def verify_ledger_chain() -> str:
        """Verify the journal hash chain integrity (tamper detection)."""
        headers = await auth_headers_fn()
        r = await _get("/ledger/verify", headers)
        ok = r.get("ok", r.get("valid"))
        return f"Chain valid: {ok}. Details: {r}"

    # ── Reports ───────────────────────────────────────────────

    @mcp.tool()
    async def report_trial_balance(as_of: str = "") -> str:
        """Trial balance report.

        Args:
            as_of: YYYY-MM-DD (default = today)
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if as_of:
            params["as_of"] = as_of
        r = await _get("/reports/trial-balance", headers, params)
        lines = [f"Trial balance as of {r['as_of']}"]
        for row in r["rows"]:
            lines.append(f"  {row['code']} {row['name']:30}  DR={row['debit']:>10.2f}  CR={row['credit']:>10.2f}  bal={row['balance']:>10.2f}")
        lines.append(f"  {'TOTAL':38}  DR={r['total_debit']:>10.2f}  CR={r['total_credit']:>10.2f}")
        return "\n".join(lines)

    @mcp.tool()
    async def report_income_statement(from_date: str, to_date: str) -> str:
        """Income statement (P&L) for a date range.

        Args:
            from_date: YYYY-MM-DD
            to_date: YYYY-MM-DD
        """
        headers = await auth_headers_fn()
        r = await _get("/reports/income-statement", headers,
                       {"from_date": from_date, "to_date": to_date})
        return f"Income statement {from_date} → {to_date}\n{r}"

    @mcp.tool()
    async def report_balance_sheet(as_of: str = "") -> str:
        """Balance sheet as of a date."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if as_of:
            params["as_of"] = as_of
        r = await _get("/reports/balance-sheet", headers, params)
        return f"Balance sheet as of {r.get('as_of', as_of)}\n{r}"

    @mcp.tool()
    async def report_vat_return(from_date: str, to_date: str) -> str:
        """VAT return summary for a period."""
        headers = await auth_headers_fn()
        r = await _get("/reports/vat-return", headers,
                       {"from_date": from_date, "to_date": to_date})
        return f"VAT return {from_date} → {to_date}\n{r}"

    @mcp.tool()
    async def report_ar_aging(as_of: str = "") -> str:
        """Accounts receivable aging (customers who owe us)."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if as_of:
            params["as_of"] = as_of
        r = await _get("/reports/ar-aging", headers, params)
        return f"AR aging as of {r.get('as_of', as_of)}\n{r}"

    @mcp.tool()
    async def report_ap_aging(as_of: str = "") -> str:
        """Accounts payable aging (suppliers we owe)."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if as_of:
            params["as_of"] = as_of
        r = await _get("/reports/ap-aging", headers, params)
        return f"AP aging as of {r.get('as_of', as_of)}\n{r}"

    # ── Periods ───────────────────────────────────────────────

    @mcp.tool()
    async def list_periods() -> str:
        """List accounting periods."""
        headers = await auth_headers_fn()
        rows = await _get("/periods", headers)
        if not rows:
            return "No periods."
        lines = [f"{len(rows)} period(s):"]
        for p in rows:
            status = "CLOSED" if p.get("is_closed") else "open  "
            lines.append(f"  [{status}] {p['label']}  {p['start_date']} → {p['end_date']}  id: {p['id']}")
        return "\n".join(lines)

    @mcp.tool()
    async def create_period(label: str, start_date: str, end_date: str) -> str:
        """Create an accounting period (admin only)."""
        headers = await auth_headers_fn()
        p = await _post("/periods", headers, {
            "label": label, "start_date": start_date, "end_date": end_date,
        })
        return f"Created period {p['label']} ({p['start_date']} → {p['end_date']})"

    @mcp.tool()
    async def close_period(period_id: str) -> str:
        """Close a period (locks postings in the date range). Requires zero unposted entries."""
        headers = await auth_headers_fn()
        p = await _post(f"/periods/{period_id}/close", headers)
        return f"Closed period {p['label']}"

    @mcp.tool()
    async def reopen_period(period_id: str) -> str:
        """Reopen a closed period (OWNER role only)."""
        headers = await auth_headers_fn()
        p = await _post(f"/periods/{period_id}/reopen", headers)
        return f"Reopened period {p['label']}"

    # ── Members ───────────────────────────────────────────────

    @mcp.tool()
    async def list_members() -> str:
        """List Ledger members (users with access to this org's books)."""
        headers = await auth_headers_fn()
        rows = await _get("/members", headers)
        if not rows:
            return "No members."
        lines = [f"{len(rows)} member(s):"]
        for m in rows:
            lines.append(f"  {m.get('user_id')}  role={m.get('role','?')}")
        return "\n".join(lines)

    @mcp.tool()
    async def add_member(user_id: str, role: str) -> str:
        """Add a member to the Ledger. Role: owner | admin | accountant | member."""
        headers = await auth_headers_fn()
        m = await _post("/members", headers, {"user_id": user_id, "role": role})
        return f"Added member {m.get('user_id')} as {m.get('role')}"

    @mcp.tool()
    async def update_member(user_id: str, role: str) -> str:
        """Change a member's role."""
        headers = await auth_headers_fn()
        m = await _put(f"/members/{user_id}", headers, {"role": role})
        return f"Updated {m.get('user_id')} → {m.get('role')}"

    @mcp.tool()
    async def remove_member(user_id: str) -> str:
        """Remove a member."""
        headers = await auth_headers_fn()
        await _delete(f"/members/{user_id}", headers)
        return f"Removed member {user_id}"

    @mcp.tool()
    async def list_audit_events(
        event_name: str = "",
        entity_type: str = "",
        entity_id: str = "",
        actor_user_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """List audit events (admin only), filterable.

        Args:
            event_name: Exact match (e.g. 'bill_approved', 'contact_created')
            entity_type: Exact match (e.g. 'bill', 'contact', 'invoice')
            entity_id: UUID of a specific entity
            actor_user_id: KC subject of the actor
            since: RFC3339 timestamp (inclusive)
            until: RFC3339 timestamp (exclusive)
            limit: Page size, 1..500 (default 50)
            offset: Pagination offset
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        for k, v in {"event_name": event_name, "entity_type": entity_type,
                     "entity_id": entity_id, "actor_user_id": actor_user_id,
                     "since": since, "until": until}.items():
            if v:
                params[k] = v
        r = await _get("/audit-events", headers, params)
        rows = r.get("items", []) if isinstance(r, dict) else r
        total = r.get("total") if isinstance(r, dict) else len(rows)
        if not rows:
            return "No audit events."
        lines = [f"{len(rows)} of {total} event(s) (limit={limit}, offset={offset}):"]
        for e in rows:
            lines.append(f"  {e.get('created_at','?')}  {e.get('event_name','?'):26}  {e.get('entity_type','?'):12}  actor={e.get('actor_user_id', e.get('actor_id','?'))}")
        return "\n".join(lines)

    # ── Expenses ──────────────────────────────────────────────

    @mcp.tool()
    async def list_expenses(status: str = "", account_code: str = "", account_id: str = "") -> str:
        """List expenses. status = draft | submitted | approved | posted | rejected."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if account_code or account_id:
            params["account_id"] = await _resolve_account_id(headers, account_id, account_code)
        rows = await _get("/expenses", headers, params)
        if not rows:
            return "No expenses."
        lines = [f"{len(rows)} expense(s):"]
        for e in rows:
            lines.append(
                f"  [{e.get('status','?'):9}] {e.get('expense_date','?')}  {e.get('amount','?'):>10}  "
                f"vat={e.get('vat_type','?'):10} {e.get('description','')[:50]}  id: {e['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def create_expense(
        expense_date: str,
        description: str,
        amount: str,
        vat_type: str = "standard",
        account_code: str = "",
        account_id: str = "",
        contact_name: str = "",
        contact_id: str = "",
    ) -> str:
        """Record an out-of-pocket expense. VAT is extracted from the inclusive total.

        Auto-posts the journal when a chart of accounts is seeded (DR expense
        + DR VAT_input / CR Bank). Falls to 'draft' status when chart is missing.

        Args:
            expense_date: YYYY-MM-DD
            description: What was bought
            amount: VAT-INCLUSIVE total as string (e.g. '115.00' for 15% VAT on 100)
            vat_type: standard | zero_rated | exempt | out_of_scope
            account_code: Expense account code (5xxx); auto-picks the system 'expenses' account if omitted
            account_id: Same but by UUID
            contact_name / contact_id: Supplier/payee (optional)
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "expense_date": expense_date,
            "description": description,
            "amount": amount,
            "vat_type": vat_type,
        }
        if account_code or account_id:
            body["account_id"] = await _resolve_account_id(headers, account_id, account_code)
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        e = await _post("/expenses", headers, body)
        return f"Created expense {e['id']}  status={e['status']}  amount={e['amount']}  vat={e.get('vat_amount','0.00')}"

    @mcp.tool()
    async def get_expense(expense_id: str) -> str:
        """Fetch one expense."""
        headers = await auth_headers_fn()
        e = await _get(f"/expenses/{expense_id}", headers)
        lines = [
            f"Expense {e['id']}  [{e.get('status','?')}]",
            f"  date: {e.get('expense_date')}  amount: {e.get('amount')}  vat: {e.get('vat_amount')} ({e.get('vat_type')})",
            f"  account_id: {e.get('account_id') or '-'}  contact_id: {e.get('contact_id') or '-'}",
            f"  description: {e.get('description','')}",
        ]
        if e.get("journal_entry_id"):
            lines.append(f"  journal_entry_id: {e['journal_entry_id']}")
        if e.get("receipt_key"):
            lines.append(f"  receipt_key: {e['receipt_key']}")
        return "\n".join(lines)

    @mcp.tool()
    async def update_expense(
        expense_id: str,
        expense_date: str = "",
        description: str = "",
        amount: str = "",
        vat_type: str = "",
        account_code: str = "",
        account_id: str = "",
        contact_name: str = "",
        contact_id: str = "",
    ) -> str:
        """Update an expense. Only legal in draft/rejected state."""
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        for k, v in {"expense_date": expense_date, "description": description,
                     "amount": amount, "vat_type": vat_type}.items():
            if v:
                body[k] = v
        if account_code or account_id:
            body["account_id"] = await _resolve_account_id(headers, account_id, account_code)
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        if not body:
            return "Nothing to update."
        e = await _put(f"/expenses/{expense_id}", headers, body)
        return f"Updated expense {e['id']}  status={e['status']}"

    @mcp.tool()
    async def submit_expense(expense_id: str) -> str:
        """Submit a draft expense for approval. Status draft → submitted."""
        headers = await auth_headers_fn()
        e = await _post(f"/expenses/{expense_id}/submit", headers)
        return f"Submitted expense {e['id']}  status={e['status']}"

    @mcp.tool()
    async def approve_expense(expense_id: str) -> str:
        """Approve a submitted expense (owner/admin). Posts the journal
        and moves to 'posted' status."""
        headers = await auth_headers_fn()
        e = await _post(f"/expenses/{expense_id}/approve", headers)
        return f"Approved expense {e['id']}  status={e['status']}"

    @mcp.tool()
    async def reject_expense(expense_id: str, reason: str = "") -> str:
        """Reject a submitted expense. Status submitted → rejected."""
        headers = await auth_headers_fn()
        path = f"/expenses/{expense_id}/reject"
        if reason:
            path += f"?reason={reason}"
        e = await _post(path, headers)
        return f"Rejected expense {e['id']}"

    @mcp.tool()
    async def upload_expense_receipt(expense_id: str, file_path: str) -> str:
        """Attach a receipt image / PDF to an expense (max 10MB).

        Args:
            expense_id: UUID of the expense
            file_path: Absolute path to the receipt file on this machine
        """
        p = Path(file_path).expanduser()
        if not p.exists():
            raise Exception(f"File not found: {p}")
        if p.stat().st_size > 10 * 1024 * 1024:
            raise Exception("Receipt must be under 10MB.")
        mime = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".heic": "image/heic",
        }.get(p.suffix.lower(), "application/octet-stream")
        headers = await auth_headers_fn()
        with p.open("rb") as fh:
            files = {"file": (p.name, fh.read(), mime)}
        e = await _post_multipart(f"/expenses/{expense_id}/receipt", headers, files)
        return f"Uploaded receipt → expense {e['id']} (key: {e.get('receipt_key','?')})"

    # ── Quotes ────────────────────────────────────────────────

    @mcp.tool()
    async def list_quotes(status: str = "", contact_name: str = "", contact_id: str = "") -> str:
        """List quotes. status = draft | sent | viewed | approved | declined | expired."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if contact_name or contact_id:
            params["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        rows = await _get("/quotes", headers, params)
        if not rows:
            return "No quotes."
        lines = [f"{len(rows)} quote(s):"]
        for q in rows:
            lines.append(
                f"  [{q.get('status','?'):9}] {q.get('quote_number','?')}  {q.get('quote_date','?')}  total={q.get('total','?')}  id: {q['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def create_quote(
        quote_date: str,
        lines: list[dict],
        contact_name: str = "",
        contact_id: str = "",
        expires_at: str = "",
        currency: str = "",
        notes: str = "",
    ) -> str:
        """Create a draft quote.

        Args:
            quote_date: YYYY-MM-DD
            lines: [{description, quantity, unit_price, vat_rate?, account_id?}] — ≥1. vat_rate defaults to 15.
            contact_name / contact_id: Customer (optional on creation; required before sending)
            expires_at: YYYY-MM-DD
            currency: ISO code (overrides org base)
            notes: Free-form notes printed on PDF
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {"quote_date": quote_date, "lines": lines}
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        if expires_at:
            body["expires_at"] = expires_at
        if currency:
            body["currency"] = currency
        if notes:
            body["notes"] = notes
        r = await _post("/quotes", headers, body)
        q = r.get("quote", r)
        return f"Created draft quote {q.get('quote_number','?')}  total={q.get('total','?')}  (id: {q.get('id')})"

    @mcp.tool()
    async def get_quote(quote_id: str) -> str:
        """Fetch a quote with lines + event history."""
        headers = await auth_headers_fn()
        r = await _get(f"/quotes/{quote_id}", headers)
        q = r.get("quote", r)
        rows = r.get("lines") or []
        events = r.get("events") or []
        lines = [
            f"Quote {q.get('quote_number','?')}  [{q.get('status','?')}]",
            f"  date: {q.get('quote_date')}  expires: {q.get('expires_at') or '-'}",
            f"  contact_id: {q.get('contact_id') or '-'}  currency: {q.get('currency') or 'ZAR'}",
            f"  subtotal: {q.get('subtotal')}  vat: {q.get('vat_amount')}  total: {q.get('total')}",
        ]
        for l in rows:
            lines.append(f"    - {l.get('description','')}  qty={l.get('quantity')}  unit={l.get('unit_price')}  line={l.get('line_total')}")
        if events:
            lines.append(f"  {len(events)} event(s):")
            for e in events[-5:]:
                lines.append(f"    {e.get('created_at','?')}  {e.get('event_type','?')}  by {e.get('actor_id','?')}")
        return "\n".join(lines)

    @mcp.tool()
    async def delete_quote(quote_id: str) -> str:
        """Delete a quote (only legal in draft status)."""
        headers = await auth_headers_fn()
        await _delete(f"/quotes/{quote_id}", headers)
        return f"Deleted quote {quote_id}"

    @mcp.tool()
    async def mark_quote_sent(quote_id: str, note: str = "") -> str:
        """Mark a quote as sent (draft → sent). Does NOT email — see send_quote for that."""
        headers = await auth_headers_fn()
        q = await _post(f"/quotes/{quote_id}/mark-sent", headers, {"note": note} if note else {})
        return f"Marked quote {q.get('quote_number','?')} as sent."

    @mcp.tool()
    async def mark_quote_approved(quote_id: str, note: str = "") -> str:
        """Record owner-side approval of a quote (sent/viewed → approved).

        For client-side approvals via magic link, use the public by-token
        endpoints from the frontend. This tool is for 'customer called me and
        said yes' cases.
        """
        headers = await auth_headers_fn()
        q = await _post(f"/quotes/{quote_id}/mark-approved", headers, {"note": note} if note else {})
        return f"Approved quote {q.get('quote_number','?')}."

    @mcp.tool()
    async def mark_quote_declined(quote_id: str, note: str = "") -> str:
        """Record owner-side decline of a quote (sent/viewed → declined)."""
        headers = await auth_headers_fn()
        q = await _post(f"/quotes/{quote_id}/mark-declined", headers, {"note": note} if note else {})
        return f"Declined quote {q.get('quote_number','?')}."

    @mcp.tool()
    async def send_quote(quote_id: str) -> str:
        """Render the quote PDF, email it via Outbox, and transition to 'sent'.

        Requires the contact to have an email address. Includes a magic link
        for the customer to view/approve/decline online.
        """
        headers = await auth_headers_fn()
        r = await _post(f"/quotes/{quote_id}/send", headers)
        return f"Sent to {r.get('to','?')}  magic_link={r.get('magic_link','?')}"

    @mcp.tool()
    async def convert_quote_to_job_card(quote_id: str) -> str:
        """Spin up a job card from an approved quote (fixed billing on quote total)."""
        headers = await auth_headers_fn()
        jc = await _post(f"/quotes/{quote_id}/convert-to-job-card", headers)
        return f"Created job card {jc.get('job_number','?')} (id: {jc.get('id')}) from quote {quote_id}"

    # ── Job cards ─────────────────────────────────────────────

    @mcp.tool()
    async def list_job_cards(status: str = "", contact_name: str = "", contact_id: str = "") -> str:
        """List job cards. status = active | paused | completed | cancelled."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if contact_name or contact_id:
            params["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        rows = await _get("/job-cards", headers, params)
        if not rows:
            return "No job cards."
        lines = [f"{len(rows)} job card(s):"]
        for jc in rows:
            billing = jc.get("billing_type", "?")
            rate = f" @ {jc.get('hourly_rate')}/h" if billing == "hourly" else f" fixed={jc.get('fixed_amount')}"
            lines.append(
                f"  [{jc.get('status','?'):9}] {jc.get('job_number','?')}  {billing:6}{rate}  {jc.get('title','')[:50]}  id: {jc['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def create_job_card(
        title: str,
        billing_type: str,
        contact_name: str = "",
        contact_id: str = "",
        quote_id: str = "",
        description: str = "",
        hourly_rate: str = "",
        fixed_amount: str = "",
        budget_hours: str = "",
        visibility: str = "",
    ) -> str:
        """Create a job card. billing_type = hourly | fixed.

        Args:
            title: Short name shown on the board
            billing_type: hourly | fixed
            contact_name / contact_id: Customer this job is for
            quote_id: Source quote (optional)
            description: Longer description
            hourly_rate: Rate per hour (required if billing_type=hourly)
            fixed_amount: Fixed total (required if billing_type=fixed)
            budget_hours: Soft cap for burn tracking
            visibility: internal | client_visible
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {"title": title, "billing_type": billing_type}
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        if quote_id:
            body["quote_id"] = quote_id
        if description:
            body["description"] = description
        if hourly_rate:
            body["hourly_rate"] = hourly_rate
        if fixed_amount:
            body["fixed_amount"] = fixed_amount
        if budget_hours:
            body["budget_hours"] = budget_hours
        if visibility:
            body["visibility"] = visibility
        jc = await _post("/job-cards", headers, body)
        return f"Created job card {jc.get('job_number','?')} (id: {jc['id']})"

    @mcp.tool()
    async def get_job_card(job_card_id: str) -> str:
        """Fetch one job card."""
        headers = await auth_headers_fn()
        jc = await _get(f"/job-cards/{job_card_id}", headers)
        lines = [
            f"Job card {jc.get('job_number','?')}  [{jc.get('status','?')}]",
            f"  title: {jc.get('title','')}",
            f"  billing: {jc.get('billing_type')}  rate: {jc.get('hourly_rate') or '-'}  fixed: {jc.get('fixed_amount') or '-'}  budget_h: {jc.get('budget_hours') or '-'}",
            f"  contact_id: {jc.get('contact_id') or '-'}  quote_id: {jc.get('quote_id') or '-'}",
            f"  visibility: {jc.get('visibility') or '-'}",
        ]
        if jc.get("description"):
            lines.append(f"  description: {jc['description']}")
        return "\n".join(lines)

    @mcp.tool()
    async def delete_job_card(job_card_id: str) -> str:
        """Delete a job card. Only legal with no billed time entries."""
        headers = await auth_headers_fn()
        await _delete(f"/job-cards/{job_card_id}", headers)
        return f"Deleted job card {job_card_id}"

    @mcp.tool()
    async def list_job_card_time_entries(job_card_id: str, include_billed: bool = False) -> str:
        """List time entries against a job card. Defaults to unbilled only."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if include_billed:
            params["include_billed"] = "true"
        rows = await _get(f"/job-cards/{job_card_id}/time-entries", headers, params)
        if not rows:
            return "No time entries."
        lines = [f"{len(rows)} entry/ies:"]
        for e in rows:
            billed = " [BILLED]" if e.get("invoice_id") else ""
            lines.append(
                f"  {e.get('entry_date','?')}  {e.get('hours','?'):>6} h  {e.get('description','') or '-':50}{billed}  id: {e['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def create_job_card_time_entry(
        job_card_id: str,
        entry_date: str,
        hours: str,
        description: str = "",
    ) -> str:
        """Log hours against an hourly job card.

        Args:
            job_card_id: UUID of the job card
            entry_date: YYYY-MM-DD
            hours: Decimal hours as string (e.g. '2.5')
            description: Optional note
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {"entry_date": entry_date, "hours": hours}
        if description:
            body["description"] = description
        e = await _post(f"/job-cards/{job_card_id}/time-entries", headers, body)
        return f"Logged {e.get('hours')} h on {e.get('entry_date')} (id: {e['id']})"

    @mcp.tool()
    async def update_job_card_time_entry(
        job_card_id: str,
        entry_id: str,
        entry_date: str = "",
        hours: str = "",
        description: str = "",
    ) -> str:
        """Edit a time entry (only legal while unbilled)."""
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        for k, v in {"entry_date": entry_date, "hours": hours, "description": description}.items():
            if v:
                body[k] = v
        if not body:
            return "Nothing to update."
        e = await _patch(f"/job-cards/{job_card_id}/time-entries/{entry_id}", headers, body)
        return f"Updated time entry {e.get('id')}"

    @mcp.tool()
    async def delete_job_card_time_entry(job_card_id: str, entry_id: str) -> str:
        """Delete a time entry (only legal while unbilled)."""
        headers = await auth_headers_fn()
        await _delete(f"/job-cards/{job_card_id}/time-entries/{entry_id}", headers)
        return f"Deleted time entry {entry_id}"

    @mcp.tool()
    async def invoice_from_job_card(
        job_card_id: str,
        from_date: str = "",
        to_date: str = "",
        group_by: str = "",
    ) -> str:
        """Create an invoice from a job card.

        Hourly: pulls unbilled time entries in the date range, groups by
        group_by, and bills at the hourly rate. Fixed: creates a single-line
        invoice for the fixed amount.

        Args:
            job_card_id: UUID of the job card
            from_date / to_date: YYYY-MM-DD, both optional (defaults to all unbilled)
            group_by: none (one line per entry) | week | month
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        for k, v in {"from_date": from_date, "to_date": to_date, "group_by": group_by}.items():
            if v:
                body[k] = v
        r = await _post(f"/job-cards/{job_card_id}/invoice", headers, body)
        n = r.get("entries_billed")
        return f"Created invoice {r.get('invoice_id')}" + (f" from {n} entries" if n is not None else "")

    # ── Purchase orders ───────────────────────────────────────

    @mcp.tool()
    async def list_purchase_orders(status: str = "", contact_name: str = "", contact_id: str = "") -> str:
        """List POs. status = draft | sent | acknowledged | received | cancelled."""
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if contact_name or contact_id:
            params["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        rows = await _get("/purchase-orders", headers, params)
        if not rows:
            return "No purchase orders."
        lines = [f"{len(rows)} PO(s):"]
        for po in rows:
            lines.append(
                f"  [{po.get('status','?'):12}] {po.get('po_number','?')}  {po.get('po_date','?')}  total={po.get('total','?')}  id: {po['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def create_purchase_order(
        po_date: str,
        lines: list[dict],
        contact_name: str = "",
        contact_id: str = "",
        expected_delivery_date: str = "",
        currency: str = "",
        notes: str = "",
        delivery_address: str = "",
    ) -> str:
        """Create a draft purchase order.

        Args:
            po_date: YYYY-MM-DD
            lines: [{description, quantity, unit_price, vat_rate?, account_id?}] — ≥1. vat_rate defaults to 15.
            contact_name / contact_id: Supplier (optional on creation; required before sending)
            expected_delivery_date: YYYY-MM-DD
            currency: ISO code
            notes: Printed on PDF
            delivery_address: Printed on PDF
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {"po_date": po_date, "lines": lines}
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        if expected_delivery_date:
            body["expected_delivery_date"] = expected_delivery_date
        if currency:
            body["currency"] = currency
        if notes:
            body["notes"] = notes
        if delivery_address:
            body["delivery_address"] = delivery_address
        r = await _post("/purchase-orders", headers, body)
        po = r.get("po", r)
        return f"Created PO {po.get('po_number','?')}  total={po.get('total','?')}  (id: {po['id']})"

    @mcp.tool()
    async def get_purchase_order(po_id: str) -> str:
        """Fetch a PO with lines."""
        headers = await auth_headers_fn()
        r = await _get(f"/purchase-orders/{po_id}", headers)
        po = r.get("po", r)
        rows = r.get("lines") or []
        lines = [
            f"PO {po.get('po_number','?')}  [{po.get('status','?')}]",
            f"  date: {po.get('po_date')}  expected: {po.get('expected_delivery_date') or '-'}",
            f"  contact_id: {po.get('contact_id') or '-'}  currency: {po.get('currency') or 'ZAR'}",
            f"  subtotal: {po.get('subtotal')}  vat: {po.get('vat_amount')}  total: {po.get('total')}",
        ]
        for l in rows:
            lines.append(f"    - {l.get('description','')}  qty={l.get('quantity')}  unit={l.get('unit_price')}  line={l.get('line_total')}")
        if po.get("delivery_address"):
            lines.append(f"  delivery: {po['delivery_address']}")
        return "\n".join(lines)

    @mcp.tool()
    async def delete_purchase_order(po_id: str) -> str:
        """Delete a PO (only legal in draft status)."""
        headers = await auth_headers_fn()
        await _delete(f"/purchase-orders/{po_id}", headers)
        return f"Deleted PO {po_id}"

    @mcp.tool()
    async def mark_po_sent(po_id: str, note: str = "") -> str:
        """Mark a PO as sent (draft → sent)."""
        headers = await auth_headers_fn()
        po = await _post(f"/purchase-orders/{po_id}/mark-sent", headers, {"note": note} if note else {})
        return f"Sent PO {po.get('po_number','?')}"

    @mcp.tool()
    async def mark_po_received(po_id: str, note: str = "") -> str:
        """Mark a PO as received (sent/acknowledged → received)."""
        headers = await auth_headers_fn()
        po = await _post(f"/purchase-orders/{po_id}/mark-received", headers, {"note": note} if note else {})
        return f"Received PO {po.get('po_number','?')}"

    @mcp.tool()
    async def cancel_purchase_order(po_id: str, note: str = "") -> str:
        """Cancel a PO (draft/sent/acknowledged → cancelled)."""
        headers = await auth_headers_fn()
        po = await _post(f"/purchase-orders/{po_id}/cancel", headers, {"note": note} if note else {})
        return f"Cancelled PO {po.get('po_number','?')}"

    # ── Cashflow + Runway + Budget review ─────────────────────

    @mcp.tool()
    async def report_cashflow(from_date: str = "", to_date: str = "") -> str:
        """Cashflow statement split by operating / investing / financing sections.

        Direct-method cashflow built from the cashbook — categorised bank txns
        are bucketed by alloc_type (debtor/creditor/revenue/expense → operating)
        or by the target account's type for other_in/other_out (asset → investing,
        equity/liability → financing).

        Args:
            from_date / to_date: YYYY-MM-DD (both optional — defaults to YTD)
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        r = await _get("/reports/cashflow", headers, params)
        lines = [f"Cashflow  {r.get('from_date','?')} → {r.get('to_date','?')}"]
        for section in ("operating", "investing", "financing"):
            rows = r.get(section) or []
            net = r.get(f"net_{section}", 0)
            lines.append(f"  {section.upper()} (net {net:+.2f}):")
            for row in rows:
                lines.append(f"    {row['code']} {row['name']:30}  {row['amount']:>12.2f}")
        lines.append(f"  NET CHANGE  {r.get('net_change', 0):+.2f}")
        return "\n".join(lines)

    @mcp.tool()
    async def report_runway(months: int = 12, include_ar: bool = False) -> str:
        """Forward cash projection (fixed burn + variable burn + revenue inflow).

        Starts from current bank balance (+ optional AR) and projects monthly
        balances for N months. Returns three paths: fixed-only, expected
        (fixed + variable), and worst-case (fixed + variable + 1σ).

        Args:
            months: 1..60 (default 12)
            include_ar: Fold accounts receivable into starting cash
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {"months": months}
        if include_ar:
            params["include_ar"] = "true"
        r = await _get("/runway", headers, params)
        lines = [
            f"Starting: cash={r['starting_cash']}  AR={r['starting_ar']}  include_ar={r['include_ar']}",
            f"Burn/mo: fixed={r['fixed_burn_per_month']}  variable={r['variable_burn_per_month']} (σ={r['variable_std_per_month']})",
        ]
        for m in r.get("months", []):
            lines.append(
                f"  {m['month']}  burn fixed={m['fixed_burn']} var={m['variable_burn']} worst={m['worst_case_burn']}  "
                f"in={m['revenue_inflow']}  bal fixed={m['projected_balance_fixed_only']} exp={m['projected_balance_expected']} worst={m['projected_balance_worst_case']}"
            )
        ends = r.get("runway_ends") or {}
        lines.append(
            f"Runway ends — fixed_only: {ends.get('fixed_only') or '—'}  "
            f"expected: {ends.get('expected') or '—'}  worst_case: {ends.get('worst_case') or '—'}"
        )
        return "\n".join(lines)

    @mcp.tool()
    async def report_runway_health() -> str:
        """Data-completeness warnings that affect runway accuracy — unapproved
        AI drafts, stale bank statements, incomplete revenue forecast, etc.

        Each warning carries a frontend CTA href so a human can act on it.
        """
        headers = await auth_headers_fn()
        r = await _get("/runway/health", headers)
        warnings = r.get("warnings") or []
        if not warnings:
            return "No runway health warnings — looking good."
        lines = [f"{len(warnings)} warning(s):"]
        for w in warnings:
            lines.append(f"  [{w.get('kind','?'):22}] {w.get('message','?')}  → {w.get('cta_href','?')}")
        return "\n".join(lines)

    @mcp.tool()
    async def report_budget_review(from_date: str = "", to_date: str = "") -> str:
        """Budget vs actual per account for a date range.

        Pulls the budget_lines forecast (monthly) and multiplies by the number
        of months in the range to produce the budgeted figure, then compares to
        actual postings. Variance = actual - budgeted (positive = over budget).

        Args:
            from_date / to_date: YYYY-MM-DD (both optional — defaults to YTD)
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        r = await _get("/budget-review", headers, params)
        rows = r.get("rows") or []
        lines = [f"Budget vs actual  {r.get('from','?')} → {r.get('to','?')}  (months={r.get('months_in_range','?')})"]
        for row in rows:
            lines.append(
                f"  {row['account_code']} {row['account_name']:30}  budget={row['budgeted']:>10}  actual={row['actual']:>10}  var={row['variance']:>+10}"
            )
        return "\n".join(lines)

    # ── Budget lines ──────────────────────────────────────────

    @mcp.tool()
    async def list_budget_lines() -> str:
        """List budget lines (one per expense account). Shows forecast,
        override, effective amount, and cost type (fixed / variable)."""
        headers = await auth_headers_fn()
        rows = await _get("/budget-lines", headers)
        if not rows:
            return "No budget lines — call refresh_budget_lines to seed them."
        lines = [f"{len(rows)} budget line(s):"]
        for b in rows:
            override = f" (override={b['amount_override']})" if b.get("amount_override") not in (None, "") else ""
            ct = b.get("cost_type_effective") or b.get("cost_type_auto") or "?"
            lines.append(
                f"  {b['account_code']} {b['account_name']:30}  eff={b['amount_effective']:>10}{override}  type={ct:8}  conf={b.get('confidence',0):.2f}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def patch_budget_line(
        budget_line_id: str,
        amount_override: str = "",
        cost_type_override: str = "",
        clear_amount_override: bool = False,
        clear_cost_type_override: bool = False,
    ) -> str:
        """Override a budget line's amount or cost_type (owner/admin).

        Pass clear_*_override=True to wipe an existing override and fall back
        to the auto-forecast value.
        """
        import json as _json
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        if clear_amount_override:
            body["amount_override"] = _json.loads("null")
        elif amount_override:
            body["amount_override"] = amount_override
        if clear_cost_type_override:
            body["cost_type_override"] = _json.loads("null")
        elif cost_type_override:
            body["cost_type_override"] = cost_type_override
        if not body:
            return "Nothing to update."
        b = await _patch(f"/budget-lines/{budget_line_id}", headers, body)
        return f"Updated {b['account_code']} {b['account_name']}  eff={b['amount_effective']}  type={b.get('cost_type_effective') or '?'}"

    @mcp.tool()
    async def refresh_budget_lines() -> str:
        """Auto-forecast every expense account from 6 months of history.

        Reads posted expense journals, computes mean + coefficient of variation,
        classifies each account as fixed or variable, and upserts rows —
        preserving any user-set overrides. Confidence rises with more history.
        """
        headers = await auth_headers_fn()
        rows = await _post("/budget-lines/refresh", headers)
        if not rows:
            return "Refreshed — no budget lines returned."
        return f"Refreshed {len(rows)} budget line(s)."

    # ── Revenue forecast ──────────────────────────────────────

    @mcp.tool()
    async def list_revenue_forecast(from_date: str = "", to_date: str = "") -> str:
        """List revenue forecast rows (booked + expected).

        Args:
            from_date / to_date: YYYY-MM-DD (both optional)
        """
        headers = await auth_headers_fn()
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        rows = await _get("/revenue-forecast", headers, params)
        if not rows:
            return "No forecast rows."
        lines = [f"{len(rows)} forecast row(s):"]
        for f in rows:
            lines.append(
                f"  [{f.get('confidence','?'):8}] {f.get('expected_date','?')}  {f.get('amount','?'):>10}  {f.get('description','')[:50]}  id: {f['id']}"
            )
        return "\n".join(lines)

    @mcp.tool()
    async def create_revenue_forecast(
        description: str,
        expected_date: str,
        amount: str,
        contact_name: str = "",
        contact_id: str = "",
        confidence: str = "expected",
        parent_id: str = "",
    ) -> str:
        """Add a revenue forecast line.

        Args:
            description: What the money is for
            expected_date: YYYY-MM-DD
            amount: Expected inflow as string
            contact_name / contact_id: Customer (optional)
            confidence: booked | expected (default expected)
            parent_id: UUID of a parent forecast (for sub-items)
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {
            "description": description,
            "expected_date": expected_date,
            "amount": amount,
            "confidence": confidence,
        }
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        if parent_id:
            body["parent_id"] = parent_id
        f = await _post("/revenue-forecast", headers, body)
        return f"Created forecast {f['id']}  {f['expected_date']}  {f['amount']}  {f['description'][:50]}"

    @mcp.tool()
    async def update_revenue_forecast(
        forecast_id: str,
        description: str = "",
        expected_date: str = "",
        amount: str = "",
        contact_name: str = "",
        contact_id: str = "",
        confidence: str = "",
    ) -> str:
        """Update a forecast row."""
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        for k, v in {"description": description, "expected_date": expected_date,
                     "amount": amount, "confidence": confidence}.items():
            if v:
                body[k] = v
        if contact_name or contact_id:
            body["contact_id"] = await _resolve_contact_id(headers, contact_id, contact_name)
        if not body:
            return "Nothing to update."
        f = await _put(f"/revenue-forecast/{forecast_id}", headers, body)
        return f"Updated forecast {f['id']}"

    @mcp.tool()
    async def delete_revenue_forecast(forecast_id: str) -> str:
        """Delete a forecast row."""
        headers = await auth_headers_fn()
        await _delete(f"/revenue-forecast/{forecast_id}", headers)
        return f"Deleted forecast {forecast_id}"

    # ── Document templates (per-doc-type branding) ────────────

    _DOC_TYPES = ("invoice", "quote", "job_card", "purchase_order", "credit_note")

    def _check_doc_type(dt: str) -> None:
        if dt not in _DOC_TYPES:
            raise Exception(f"Invalid doc_type '{dt}'. Expected one of: {', '.join(_DOC_TYPES)}")

    @mcp.tool()
    async def list_document_templates() -> str:
        """List all per-doc-type branding templates configured for the org.

        Returns one line per template (invoice, quote, job_card, purchase_order,
        credit_note). Doc types without a template row are omitted.
        """
        headers = await auth_headers_fn()
        rows = await _get("/document-templates", headers)
        if not rows:
            return "No document templates configured. Use update_document_template to set one."
        lines = [f"{len(rows)} template(s):"]
        for r in rows:
            has_logo = " [logo]" if r.get("logo_key") else ""
            colour = f" colour={r['primary_color']}" if r.get("primary_color") else ""
            lines.append(f"  {r.get('doc_type','?'):15}{colour}{has_logo}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_document_template(doc_type: str) -> str:
        """Fetch the branding template for one doc type.

        Args:
            doc_type: invoice | quote | job_card | purchase_order | credit_note
        """
        _check_doc_type(doc_type)
        headers = await auth_headers_fn()
        r = await _get(f"/document-templates/{doc_type}", headers)
        lines = [f"Template: {doc_type}"]
        for k in ("primary_color", "header_text", "intro_text", "footer_text",
                  "terms_conditions", "bank_details", "payment_instructions",
                  "email_subject_template", "email_body_template"):
            v = r.get(k)
            if v:
                preview = v if len(str(v)) <= 80 else str(v)[:77] + "..."
                lines.append(f"  {k}: {preview}")
        if r.get("logo_key"):
            lines.append(f"  logo_key: {r['logo_key']}")
        return "\n".join(lines) if len(lines) > 1 else f"Template '{doc_type}' is empty (no fields set)."

    @mcp.tool()
    async def update_document_template(
        doc_type: str,
        header_text: str = "",
        intro_text: str = "",
        footer_text: str = "",
        terms_conditions: str = "",
        bank_details: str = "",
        payment_instructions: str = "",
        primary_color: str = "",
        email_subject_template: str = "",
        email_body_template: str = "",
    ) -> str:
        """Upsert branding + text for one doc type. Empty fields are left unchanged.

        Args:
            doc_type: invoice | quote | job_card | purchase_order | credit_note
            header_text: Company name / heading printed at the top of the PDF
            intro_text: Short paragraph shown above the line items
            footer_text: Footer line (e.g. registration + VAT numbers)
            terms_conditions: Free-form T&Cs printed at the bottom
            bank_details: Bank name / account / branch code for payment
            payment_instructions: E.g. "Please use invoice number as reference"
            primary_color: Hex colour (e.g. "#e25c00") used for accents in the PDF
            email_subject_template: Subject template for send-by-email flow
            email_body_template: Body template for send-by-email flow
        """
        _check_doc_type(doc_type)
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        for k, v in {
            "header_text": header_text, "intro_text": intro_text,
            "footer_text": footer_text, "terms_conditions": terms_conditions,
            "bank_details": bank_details, "payment_instructions": payment_instructions,
            "primary_color": primary_color,
            "email_subject_template": email_subject_template,
            "email_body_template": email_body_template,
        }.items():
            if v:
                body[k] = v
        if not body:
            return "Nothing to update — provide at least one field."
        r = await _put(f"/document-templates/{doc_type}", headers, body)
        return f"Updated {doc_type} template ({len(body)} field(s) set)."

    @mcp.tool()
    async def upload_document_template_logo(doc_type: str, file_path: str) -> str:
        """Upload a logo for one doc type (PNG or JPG, ≤2MB).

        The file is read from this machine and uploaded via multipart. SVG /
        GIF / WEBP are rejected — the PDF renderer only embeds PNG/JPG.

        Args:
            doc_type: invoice | quote | job_card | purchase_order | credit_note
            file_path: Absolute path to a .png / .jpg / .jpeg on this machine
        """
        _check_doc_type(doc_type)
        p = Path(file_path).expanduser()
        if not p.exists():
            raise Exception(f"File not found: {p}")
        ext = p.suffix.lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext)
        if not mime:
            raise Exception(f"Unsupported logo format '{ext}'. Use .png, .jpg or .jpeg.")
        if p.stat().st_size > 2 * 1024 * 1024:
            raise Exception("Logo must be under 2MB.")
        headers = await auth_headers_fn()
        with p.open("rb") as fh:
            files = {"file": (p.name, fh.read(), mime)}
        r = await _post_multipart(f"/document-templates/{doc_type}/logo", headers, files)
        return f"Uploaded logo for {doc_type} template (key: {r.get('logo_key','?')})."

    @mcp.tool()
    async def delete_document_template_logo(doc_type: str) -> str:
        """Remove the logo from one doc type's template.

        Args:
            doc_type: invoice | quote | job_card | purchase_order | credit_note
        """
        _check_doc_type(doc_type)
        headers = await auth_headers_fn()
        await _delete(f"/document-templates/{doc_type}/logo", headers)
        return f"Cleared logo for {doc_type} template."

    # ── Ledger settings (org-level config) ────────────────────

    @mcp.tool()
    async def get_settings() -> str:
        """Fetch org-level Ledger settings (VAT, currency, financial year, AI auto-approve)."""
        headers = await auth_headers_fn()
        r = await _get("/settings", headers)
        lines = ["Ledger settings:"]
        for k in ("org_trading_name", "org_registration_number",
                  "company_address", "company_email", "company_phone",
                  "base_currency", "default_vat_rate",
                  "vat_registered", "vat_number", "vat_category", "vat_period_start",
                  "financial_year_end",
                  "ai_auto_approve_enabled", "ai_auto_approve_confidence_threshold"):
            if k in r:
                lines.append(f"  {k}: {r[k]}")
        return "\n".join(lines)

    @mcp.tool()
    async def update_settings(
        default_vat_rate: str = "",
        vat_registered: bool | None = None,
        vat_number: str = "",
        vat_category: str = "",
        vat_period_start: str = "",
        financial_year_end: str = "",
        base_currency: str = "",
        org_trading_name: str = "",
        org_registration_number: str = "",
        company_address: str = "",
        company_email: str = "",
        company_phone: str = "",
        ai_auto_approve_enabled: bool | None = None,
        ai_auto_approve_confidence_threshold: str = "",
    ) -> str:
        """Patch org Ledger settings. Empty strings / None are left unchanged.

        Args:
            default_vat_rate: Default VAT rate as decimal string (e.g. "0.15" for 15%)
            vat_registered: Whether the org is VAT-registered
            vat_number: SARS VAT registration number
            vat_category: monthly | cat_a | cat_b | cat_c | annual
            vat_period_start: First day of the current VAT period, YYYY-MM-DD
            financial_year_end: Financial year end, MM-DD (e.g. "02-28")
            base_currency: 3-letter ISO code (e.g. "ZAR")
            org_trading_name: Legal trading name printed on documents
            org_registration_number: Company registration number
            company_address: Registered office address (multiline; \n between lines) — appears in the supplier block on invoices/quotes
            company_email: Supplier contact email printed in the supplier block
            company_phone: Supplier contact phone printed in the supplier block
            ai_auto_approve_enabled: Auto-approve AI-drafted bills above the confidence threshold
            ai_auto_approve_confidence_threshold: Confidence floor as decimal string (e.g. "0.85")
        """
        headers = await auth_headers_fn()
        body: dict[str, Any] = {}
        for k, v in {
            "vat_number": vat_number, "vat_category": vat_category,
            "vat_period_start": vat_period_start, "financial_year_end": financial_year_end,
            "base_currency": base_currency, "org_trading_name": org_trading_name,
            "org_registration_number": org_registration_number,
            "company_address": company_address, "company_email": company_email,
            "company_phone": company_phone,
        }.items():
            if v:
                body[k] = v
        if default_vat_rate:
            body["default_vat_rate"] = float(default_vat_rate)
        if ai_auto_approve_confidence_threshold:
            body["ai_auto_approve_confidence_threshold"] = float(ai_auto_approve_confidence_threshold)
        if vat_registered is not None:
            body["vat_registered"] = vat_registered
        if ai_auto_approve_enabled is not None:
            body["ai_auto_approve_enabled"] = ai_auto_approve_enabled
        if not body:
            return "Nothing to update — provide at least one field."
        r = await _put("/settings", headers, body)
        return f"Updated settings ({len(body)} field(s)). Current: base_currency={r.get('base_currency')}, default_vat_rate={r.get('default_vat_rate')}, vat_registered={r.get('vat_registered')}"
