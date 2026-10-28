"""Migration guard (Pro): a Claude Code PreToolUse hook for shell commands.

The MCP server only sees SQL sent through it. Agents also run ORM/migration
CLIs (``prisma migrate reset --force`` and friends) that wipe data without
touching pgbrake. This hook catches those commands, takes a pg_dump snapshot
first when a DSN is configured, and asks the human before letting it run.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import license as lic

# (pattern, human reason). Matched case-insensitively against the whole command.
RULES: list[tuple[str, str]] = [
    (r"\bprisma\s+migrate\s+reset\b", "prisma migrate reset drops the database"),
    (r"\bprisma\s+db\s+push\b.*--(force-reset|accept-data-loss)", "prisma db push may drop data"),
    (r"\bdrizzle-kit\s+(drop|push\b.*--force)", "drizzle-kit can drop tables/columns"),
    (r"\balembic\s+downgrade\b", "alembic downgrade reverses migrations"),
    (r"\b(rails|rake)\s+db:(drop|reset|schema:load|structure:load|migrate:reset|rollback|purge|truncate_all)\b",
     "rails db task destroys or rewinds data"),
    (r"\bmanage\.py\s+(flush|reset_db|sqlflush)\b", "django command wipes data"),
    (r"\bmanage\.py\s+migrate\s+\S+\s+zero\b", "django migrate <app> zero unapplies every migration"),
    (r"\bsequelize(-cli)?\s+db:(migrate:undo|drop|seed:undo)", "sequelize undo/drop"),
    (r"\bknex\s+migrate:(rollback|down)\b", "knex rollback"),
    (r"\btypeorm\s+(schema:drop|migration:revert)\b", "typeorm drop/revert"),
    (r"\bsupabase\s+db\s+reset\b", "supabase db reset wipes the local database"),
    (r"\bflyway\b.*\bclean\b", "flyway clean drops all objects"),
    (r"\b(dropdb|pg_restore\b.*--clean)\b", "drops or overwrites a database"),
    (r"\bpsql\b.*\b(drop\s+(table|schema|database)|truncate)\b", "destructive SQL via psql bypasses pgbrake"),
]


def match(command: str) -> str | None:
    for pat, why in RULES:
        if re.search(pat, command, re.IGNORECASE | re.DOTALL):
            return why
    return None


def snapshot(dsn: str) -> tuple[str | None, str]:
    """pg_dump the database before the command runs. Returns (path, note)."""
    if not shutil.which("pg_dump"):
        return None, "pg_dump not found, no snapshot taken"
    d = Path(os.environ.get("PGBRAKE_HOME", Path.home() / ".pgbrake")) / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"snap_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.dump"
    try:
        subprocess.run(["pg_dump", "--format=custom", f"--file={out}", dsn],
                       check=True, capture_output=True, timeout=600)
    except Exception as e:  # noqa: BLE001
        return None, f"snapshot failed: {getattr(e, 'stderr', b'') or e}"[:300]
    return str(out), f"snapshot saved: {out} (restore: pg_restore --clean -d <db> {out})"


def decide(command: str, *, pro: bool | None = None, dsn: str | None = None) -> dict | None:
    """Return the hook JSON for a command, or None to let it through untouched."""
    why = match(command)
    if not why:
        return None
    pro = lic.is_pro() if pro is None else pro
    if not pro:
        msg = (f"pgbrake: this looks destructive ({why}). The migration guard is a pgbrake Pro "
               f"feature: {lic.BUY_URL}")
        return {"systemMessage": msg}
    note = "no PGBRAKE_DSN set, no snapshot taken"
    if dsn:
        _, note = snapshot(dsn)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": f"pgbrake: {why}. {note}",
    }}


def hook_main(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        event = json.load(stdin)
    except Exception:
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    cmd = (event.get("tool_input") or {}).get("command", "")
    out = decide(cmd, dsn=os.environ.get("PGBRAKE_DSN") or os.environ.get("DATABASE_URL"))
    if out:
        json.dump(out, stdout)
    return 0


def install(settings_path: str | Path = ".claude/settings.json") -> Path:
    """Add the hook to a Claude Code settings file (project-level by default)."""
    p = Path(settings_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": "pgbrake guard-hook"}]}
    if not any(h.get("command") == "pgbrake guard-hook"
               for e in pre for h in e.get("hooks", [])):
        pre.append(entry)
    p.write_text(json.dumps(data, indent=2) + "\n")
    return p
