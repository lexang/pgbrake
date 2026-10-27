"""MCP server exposing pgbrake tools over stdio."""
from __future__ import annotations

import json
import os

from pydantic import BaseModel, Field

try:  # mcp >= 2
    from mcp.server.mcpserver import Context, MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP as _Server

from .engine import Brake

INSTRUCTIONS = """pgbrake guards a Postgres database. Use `query` for reads and `execute` for writes.
Writes return a change id that `undo` can reverse. Risky SQL (DDL, writes without WHERE,
writes over the row limit) is held for HUMAN approval: tell the user the approval id and
wait; never try to approve it yourself or work around the brake with other tools."""


class Approve(BaseModel):
    approve: bool = Field(description="Run this SQL against the database?")


def build(dsn: str | None = None) -> _Server:
    dsn = dsn or os.environ.get("PGBRAKE_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("pgbrake: set PGBRAKE_DSN (or DATABASE_URL) or pass --dsn")
    brake = Brake(dsn, client=os.environ.get("PGBRAKE_CLIENT", "mcp"))
    mcp = _Server("pgbrake", instructions=INSTRUCTIONS)

    def dump(r):
        return json.dumps(r.to_dict(), ensure_ascii=False, indent=1)

    @mcp.tool(description="Run a read-only SQL query (SELECT/EXPLAIN). Writes are rejected here.")
    def query(sql: str) -> str:
        brake.policy.read_only, prev = True, brake.policy.read_only
        try:
            return dump(brake.run(sql))
        finally:
            brake.policy.read_only = prev

    @mcp.tool(description="Run SQL that may modify data. Returns rows, a change id for undo, or an "
                          "approval id when a human must approve first.")
    async def execute(sql: str, ctx: Context, approval_id: str | None = None) -> str:
        r = brake.run(sql, approval_id=approval_id)
        if r.status == "needs_approval" and r.pending_id and not approval_id:
            try:  # ask the human directly if the client supports elicitation
                ans = await ctx.elicit(
                    message=f"pgbrake is holding this SQL ({'; '.join(r.reasons)}):\n\n{sql}", schema=Approve)
                if getattr(ans, "action", None) == "accept" and ans.data and ans.data.approve:
                    brake.journal.decide(r.pending_id, "approved")
                    return dump(brake.run(sql, approval_id=r.pending_id))
                if getattr(ans, "action", None) in ("decline", "accept"):
                    brake.journal.decide(r.pending_id, "denied")
                    return dump(r.__class__("denied", "the user declined this SQL", pending_id=r.pending_id))
            except Exception:
                pass  # client has no elicitation: fall back to `pgbrake approve <id>` in a terminal
        return dump(r)

    @mcp.tool(description="Reverse a change made through pgbrake, by change id.")
    def undo(change_id: str, force: bool = False) -> str:
        return dump(brake.undo(change_id, force=force))

    @mcp.tool(description="List recent changes that can be undone.")
    def list_changes(limit: int = 20) -> str:
        return json.dumps(brake.journal.changes(limit), default=str, indent=1)

    @mcp.tool(description="Check whether an approval id has been approved by a human yet.")
    def approval_status(approval_id: str) -> str:
        p = brake.journal.get_pending(approval_id)
        return json.dumps({"approval_id": approval_id, "status": p["status"] if p else "unknown"})

    return mcp


def main(dsn: str | None = None):
    build(dsn).run()
