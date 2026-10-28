"""pgbrake command line: serve the MCP server, approve held SQL, inspect and undo changes."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from .journal import Journal


def _ts(t):
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "-"


def _export(j, fmt, out):
    import csv
    rows = j.audit(None)
    f = open(out, "w", newline="") if out else sys.stdout
    try:
        if fmt == "json":
            json.dump(rows, f, indent=1, default=str)
        else:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["ts"])
            w.writeheader()
            for r in rows:
                w.writerow({**r, "ts": _ts(r.get("ts"))})
    finally:
        if out:
            f.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pgbrake", description="Safety brake for AI agents on Postgres")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the MCP server (stdio)")
    s.add_argument("--dsn")
    sub.add_parser("pending", help="list SQL waiting for approval")
    a = sub.add_parser("approve", help="approve held SQL")
    a.add_argument("id")
    d = sub.add_parser("deny", help="deny held SQL")
    d.add_argument("id")
    c = sub.add_parser("changes", help="list undoable changes")
    c.add_argument("-n", type=int, default=20)
    u = sub.add_parser("undo", help="undo a change")
    u.add_argument("id")
    u.add_argument("--dsn")
    u.add_argument("--force", action="store_true")
    lg = sub.add_parser("log", help="show the audit log")
    lg.add_argument("-n", type=int, default=30)
    lg.add_argument("--json", action="store_true")
    lg.add_argument("--export", choices=["csv", "json"], help="(Pro) export the whole audit log")
    lg.add_argument("--out", help="file for --export (default: stdout)")
    li = sub.add_parser("license", help="show or activate a Pro license")
    li.add_argument("key", nargs="?", help="license key to activate")
    sub.add_parser("guard-hook", help="(Pro) Claude Code PreToolUse hook; reads the event on stdin")
    ih = sub.add_parser("install-hook", help="(Pro) add the migration guard to Claude Code settings")
    ih.add_argument("--settings", default=".claude/settings.json")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .server import main as serve
        return serve(args.dsn)

    if args.cmd == "license":
        from . import license as lic
        if args.key:
            r = lic.activate(args.key)
            sys.exit(f"license not valid: {r.error}") if not r.valid else print(f"pgbrake Pro activated for {r.email}")
        else:
            r = lic.current()
            print(f"Pro ({r.email})" if r.valid else f"free ({r.error}). Get Pro: {lic.BUY_URL}")
        return
    if args.cmd == "guard-hook":
        from .guard import hook_main
        return hook_main()
    if args.cmd == "install-hook":
        from .guard import install
        from .license import is_pro, BUY_URL
        p = install(args.settings)
        print(f"hook added to {p}")
        if not is_pro():
            print(f"note: without a Pro license the hook only warns. Get Pro: {BUY_URL}")
        return

    j = Journal()
    if args.cmd == "pending":
        rows = j.list_pending()
        if not rows:
            print("nothing waiting")
        for p in rows:
            print(f"{p['id']}  {_ts(p['ts'])}  {', '.join(json.loads(p['reasons']))}\n    {p['sql']}\n")
    elif args.cmd in ("approve", "deny"):
        p = j.get_pending(args.id)
        if not p:
            sys.exit(f"unknown approval id {args.id}")
        print(f"SQL:\n  {p['sql']}\nReasons: {', '.join(json.loads(p['reasons']))}")
        if args.cmd == "approve":
            # Agents run shell commands without a TTY. Requiring one keeps an agent
            # from approving its own SQL via `pgbrake approve`.
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                sys.exit("pgbrake approve must be run by a human in an interactive terminal")
            if input("Approve? [y/N] ").strip().lower() != "y":
                sys.exit("not approved")
        ok = j.decide(args.id, "approved" if args.cmd == "approve" else "denied")
        print(("approved" if args.cmd == "approve" else "denied") if ok else "already decided")
    elif args.cmd == "changes":
        for ch in j.changes(args.n):
            state = f"undone {_ts(ch['undone_at'])}" if ch["undone_at"] else "undoable"
            print(f"{ch['id']}  {_ts(ch['ts'])}  {ch['op']:6} {ch['tbl']}  {ch['rows']} row(s)  [{state}]\n    {ch['sql']}")
    elif args.cmd == "undo":
        from .engine import Brake
        dsn = args.dsn or os.environ.get("PGBRAKE_DSN") or os.environ.get("DATABASE_URL")
        if not dsn:
            sys.exit("set PGBRAKE_DSN or pass --dsn")
        r = Brake(dsn, journal=j, client="cli").undo(args.id, force=args.force)
        print(r.message)
        sys.exit(0 if r.status == "ok" else 1)
    elif args.cmd == "log":
        if args.export:
            from .license import is_pro, BUY_URL
            if not is_pro():
                sys.exit(f"audit export is a pgbrake Pro feature: {BUY_URL}")
            return _export(j, args.export, args.out)
        rows = j.audit(args.n)
        if args.json:
            print(json.dumps(rows, indent=1, default=str))
        else:
            for r in reversed(rows):
                print(f"{_ts(r['ts'])}  {r['decision']:14} {r['kind'] or '':6} {r['rowcount'] if r['rowcount'] is not None else '':>5}  {r['sql'][:120]}")


if __name__ == "__main__":
    main()
