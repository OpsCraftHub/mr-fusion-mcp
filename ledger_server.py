"""Vault — MCP server for the OpsCraft Ledger (bookkeeping) service.

Exposes the full ledger-go /api/v1 surface: chart of accounts, contacts
(incl. AI-review approval + statements), cashbook (CSV import, preview,
categorise, learned rules, batches, manual entries, backfills), invoices
(CRUD + PDF via UI), credit notes (CRUD + post), bills (CRUD, AI draft
flow, void, attachments, audit), payments (create + allocate across
foreign-currency docs), journal, reports (trial balance, P&L, balance
sheet, VAT, aging, cashflow, runway, budget-vs-actual), periods, members,
audit events, document templates, settings, expenses (full approval
flow + receipts), quotes (CRUD + send + status transitions + convert to
job card), job cards (CRUD + time entries + invoice-from-timesheet),
purchase orders (CRUD + status transitions), budget lines (list / patch /
auto-refresh), revenue forecast (CRUD).
"""

from mcp.server.fastmcp import FastMCP

from auth import make_auth_headers_fn
from ledger_tools import register_ledger_tools

mcp = FastMCP("ledger")

_auth_headers = make_auth_headers_fn(static_token_var="LEDGER_TOKEN")

register_ledger_tools(mcp, _auth_headers)

if __name__ == "__main__":
    mcp.run(transport="stdio")
