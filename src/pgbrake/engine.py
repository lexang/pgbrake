"""The brake: decide, capture before-images, execute, undo.

All enforcement lives here (server side), so it holds even when the MCP
client's own permission prompts or hooks are skipped.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql as S
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .classify import Kind, Statement, classify
from .journal import Journal, sql_hash


@dataclass
class Policy:
    max_rows: int = int(os.environ.get("PGBRAKE_MAX_ROWS", "100"))
    allow_ddl: bool = os.environ.get("PGBRAKE_ALLOW_DDL", "0") == "1"
    read_only: bool = os.environ.get("PGBRAKE_READ_ONLY", "0") == "1"
    statement_timeout_ms: int = int(os.environ.get("PGBRAKE_STATEMENT_TIMEOUT_MS", "15000"))
    max_result_rows: int = 500


@dataclass
class Result:
    status: str                      # ok | needs_approval | denied | error
    message: str = ""
    rows: list[dict] = field(default_factory=list)
    rowcount: int | None = None
    change_ids: list[str] = field(default_factory=list)
    pending_id: str | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if v not in (None, [], "")}
        return json.loads(json.dumps(d, default=str))


class NeedsApproval(Exception):
    def __init__(self, reasons):
        self.reasons = reasons


class Brake:
    def __init__(self, dsn: str, journal: Journal | None = None, policy: Policy | None = None,
                 client: str | None = None):
        self.dsn = dsn
        self.journal = journal or Journal()
        self.policy = policy or Policy()
        self.client = client
        self.db_label = psycopg.conninfo.conninfo_to_dict(dsn).get("dbname") if "=" in dsn or "://" in dsn else dsn

    def _connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False,
                               options=f"-c statement_timeout={self.policy.statement_timeout_ms}")

    # ---- decision ----------------------------------------------------
    def _static_reasons(self, stmts: list[Statement]) -> tuple[list[str], list[str]]:
        deny, approve = [], []
        for st in stmts:
            if st.kind == Kind.BLOCKED:
                deny += st.reasons or [f"{st.node} blocked"]
            elif self.policy.read_only and st.kind != Kind.READ:
                deny.append(f"read-only mode: {st.node} refused")
            elif st.kind == Kind.DDL and not self.policy.allow_ddl:
                approve += st.reasons
            elif st.kind in (Kind.BULK_WRITE, Kind.OTHER):
                approve += st.reasons or [f"{st.node} needs approval"]
            elif st.kind == Kind.WRITE and not st.undo_supported:
                approve += st.reasons
        return deny, approve

    def run(self, sql: str, approval_id: str | None = None) -> Result:
        try:
            stmts = classify(sql)
        except Exception as e:  # parse error: refuse rather than guess
            r = Result("denied", f"could not parse SQL: {e}")
            self.journal.log(sql=sql, decision="denied", message=r.message, client=self.client, db=self.db_label)
            return r
        if not stmts:
            return Result("error", "empty statement")
        kind = max((s.kind.value for s in stmts), key=lambda k: list(Kind._value2member_map_).index(k))

        deny, approve = self._static_reasons(stmts)
        if deny:
            r = Result("denied", "refused by pgbrake policy", reasons=deny)
            self.journal.log(sql=sql, kind=kind, decision="denied", message="; ".join(deny),
                             client=self.client, db=self.db_label)
            return r

        approved = False
        if approval_id:
            p = self.journal.get_pending(approval_id)
            if not p or p["sql_hash"] != sql_hash(sql):
                return Result("denied", "approval id does not match this exact SQL")
            if p["status"] != "approved":
                return Result("needs_approval", f"approval {approval_id} is {p['status']}; a human must approve it",
                              pending_id=approval_id)
            if p["expires"] < time.time():
                return Result("denied", "approval expired; request a new one")
            approved = True
        elif approve:
            return self._pend(sql, kind, approve)

        try:
            return self._execute(sql, stmts, kind, approved, approval_id)
        except NeedsApproval as na:
            return self._pend(sql, kind, na.reasons)
        except psycopg.Error as e:
            msg = str(e).strip()
            self.journal.log(sql=sql, kind=kind, decision="error", message=msg, client=self.client, db=self.db_label)
            return Result("error", msg)

    def _pend(self, sql, kind, reasons) -> Result:
        pid = self.journal.add_pending(db=self.db_label, sql=sql, reasons=reasons)
        self.journal.log(sql=sql, kind=kind, decision="needs_approval", pending_id=pid,
                         message="; ".join(reasons), client=self.client, db=self.db_label)
        return Result("needs_approval",
                      f"Held for human approval. A person must run `pgbrake approve {pid}` "
                      f"(or accept the prompt in their client), then call execute again with approval_id.",
                      pending_id=pid, reasons=reasons)

    # ---- execution ---------------------------------------------------
    def _pk(self, conn, st: Statement) -> list[str]:
        rows = conn.execute(
            """SELECT a.attname FROM pg_index i
               JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey)
               WHERE i.indrelid = %s::regclass AND i.indisprimary ORDER BY a.attnum""",
            (st.qualified_table,)).fetchall()
        return [r["attname"] for r in rows]

    def _capture(self, conn, st: Statement) -> list[dict]:
        target = S.SQL(st.qualified_table)
        ref = S.Identifier(st.alias) if st.alias else target
        from_ = S.SQL("{} AS {}").format(target, S.Identifier(st.alias)) if st.alias else target
        if st.from_sql:
            from_ = S.SQL("{}, {}").format(from_, S.SQL(st.from_sql))
        q = S.SQL("SELECT to_jsonb({ref}) AS r FROM {from_} WHERE {where} FOR UPDATE OF {lock}").format(
            ref=ref, from_=from_, where=S.SQL(st.where_sql),
            lock=S.Identifier(st.alias) if st.alias else S.Identifier(st.table))
        seen, out = set(), []
        for r in conn.execute(q).fetchall():  # join-based writes can repeat target rows
            key = json.dumps(r["r"], sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                out.append(r["r"])
        return out

    def _refetch(self, conn, st: Statement, pk: list[str], rows: list[dict]) -> list[dict]:
        if not rows or not pk:
            return []
        q = S.SQL("SELECT to_jsonb(t) AS r FROM {tbl} t JOIN jsonb_populate_recordset(NULL::{tbl}, %s) k "
                  "USING ({pk})").format(tbl=S.SQL(st.qualified_table), pk=S.SQL(", ").join(map(S.Identifier, pk)))
        return [r["r"] for r in conn.execute(q, (Jsonb(rows),)).fetchall()]

    def _execute(self, sql, stmts, kind, approved, approval_id) -> Result:
        change_ids: list[str] = []
        total = 0
        out_rows: list[dict] = []
        with self._connect() as conn:
            if kind == Kind.READ.value:
                conn.read_only = True
            pending_changes = []
            for st in stmts:
                before, pk = [], []
                if st.kind in (Kind.WRITE, Kind.BULK_WRITE) and st.table and st.undo_supported:
                    pk = self._pk(conn, st)
                    if st.node in ("UpdateStmt", "DeleteStmt"):
                        if st.where_sql is None:  # bulk, approved: capture whole table
                            st.where_sql = "true"
                        before = self._capture(conn, st)
                        if len(before) > self.policy.max_rows and not approved:
                            conn.rollback()
                            raise NeedsApproval([f"{st.node[:-4].upper()} would touch {len(before)} rows "
                                                 f"(limit {self.policy.max_rows})"])
                        if st.node == "UpdateStmt" and not pk:
                            if not approved:
                                conn.rollback()
                                raise NeedsApproval([f"{st.table} has no primary key; UPDATE cannot be undone"])
                exec_sql = st.sql
                if st.node == "InsertStmt" and st.undo_supported and st.table:
                    exec_sql = f"WITH _pgbrake AS ({st.sql}{'' if st.has_returning else ' RETURNING *'}) " \
                               f"SELECT to_jsonb(_pgbrake) AS r FROM _pgbrake"
                cur = conn.execute(exec_sql)
                rc = cur.rowcount
                rows = cur.fetchall() if cur.description else []
                if st.node == "InsertStmt" and st.undo_supported and st.table:
                    inserted = [r["r"] for r in rows]
                    rows = inserted if st.has_returning else []
                    rc = len(inserted)
                    if rc > self.policy.max_rows and not approved:
                        conn.rollback()
                        raise NeedsApproval([f"INSERT of {rc} rows (limit {self.policy.max_rows})"])
                    if pk and inserted and all(k in inserted[0] for k in pk):
                        pending_changes.append(dict(tbl=st.qualified_table, op="insert", pk_cols=pk,
                                                    before=[], after=[{k: r[k] for k in pk} for r in inserted],
                                                    sql=st.sql))
                elif st.node == "UpdateStmt" and before:
                    after = self._refetch(conn, st, pk, [{k: r[k] for k in pk} for r in before])
                    pending_changes.append(dict(tbl=st.qualified_table, op="update", pk_cols=pk,
                                                before=before, after=after, sql=st.sql))
                elif st.node == "DeleteStmt" and before:
                    pending_changes.append(dict(tbl=st.qualified_table, op="delete", pk_cols=pk,
                                                before=before, after=[], sql=st.sql))
                if st.kind in (Kind.WRITE, Kind.BULK_WRITE) and rc and rc > self.policy.max_rows and not approved:
                    conn.rollback()
                    raise NeedsApproval([f"{st.node} affected {rc} rows (limit {self.policy.max_rows})"])
                total += max(rc, 0) if rc is not None else 0
                out_rows = rows[: self.policy.max_result_rows]
            conn.commit()
        for ch in pending_changes:
            change_ids.append(self.journal.add_change(db=self.db_label, **ch))
        if approval_id:
            self.journal.decide(approval_id, "used")
        self.journal.log(sql=sql, kind=kind, decision="approved" if approved else "allowed",
                         rowcount=total, change_ids=change_ids, client=self.client, db=self.db_label)
        msg = f"{total} row(s) affected" if kind != Kind.READ.value else f"{len(out_rows)} row(s)"
        if change_ids:
            msg += f"; undo with change id {', '.join(change_ids)}"
        return Result("ok", msg, rows=out_rows, rowcount=total, change_ids=change_ids)

    # ---- undo ----------------------------------------------------------
    def undo(self, change_id: str, force: bool = False) -> Result:
        ch = self.journal.get_change(change_id)
        if not ch:
            return Result("error", f"unknown change {change_id} (free tier keeps the last 20 changes)")
        if ch["undone_at"]:
            return Result("error", f"{change_id} was already undone")
        tbl, pk = S.SQL(ch["tbl"]), ch["pk_cols"]
        pkl = S.SQL(", ").join(map(S.Identifier, pk))
        with self._connect() as conn:
            if ch["op"] in ("update", "insert") and not force and ch["after"]:
                # conflict check: rows must still look the way pgbrake left them
                cur = conn.execute(S.SQL(
                    "SELECT count(*) AS n FROM {t} t JOIN jsonb_populate_recordset(NULL::{t}, %s) k USING ({pk})"
                ).format(t=tbl, pk=pkl), (Jsonb(ch["after"]),)).fetchone()["n"]
                if cur != len(ch["after"]):
                    return Result("error", f"{len(ch['after']) - cur} row(s) changed or vanished since; "
                                           f"re-run with force=true to undo anyway")
            cols = [r["attname"] for r in conn.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped "
                "AND attgenerated='' ORDER BY attnum", (ch["tbl"],)).fetchall()]
            cl = S.SQL(", ").join(map(S.Identifier, cols))
            if ch["op"] == "delete":
                q = S.SQL("INSERT INTO {t} ({c}) OVERRIDING SYSTEM VALUE SELECT {c} FROM "
                          "jsonb_populate_recordset(NULL::{t}, %s)").format(t=tbl, c=cl)
                n = conn.execute(q, (Jsonb(ch["before"]),)).rowcount
            elif ch["op"] == "update":
                setcols = [c for c in cols if c not in pk]
                q = S.SQL("UPDATE {t} AS t SET ({c}) = ({rc}) FROM jsonb_populate_recordset(NULL::{t}, %s) AS r "
                          "WHERE {on}").format(
                    t=tbl, c=S.SQL(", ").join(map(S.Identifier, setcols)),
                    rc=S.SQL(", ").join(S.SQL("r.{}").format(S.Identifier(c)) for c in setcols),
                    on=S.SQL(" AND ").join(S.SQL("t.{0} = r.{0}").format(S.Identifier(k)) for k in pk))
                n = conn.execute(q, (Jsonb(ch["before"]),)).rowcount
            else:  # insert
                q = S.SQL("DELETE FROM {t} AS t USING jsonb_populate_recordset(NULL::{t}, %s) AS r WHERE {on}").format(
                    t=tbl, on=S.SQL(" AND ").join(S.SQL("t.{0} = r.{0}").format(S.Identifier(k)) for k in pk))
                n = conn.execute(q, (Jsonb(ch["after"]),)).rowcount
            conn.commit()
        self.journal.mark_undone(change_id)
        self.journal.log(sql=f"-- undo {change_id}", kind="undo", decision="allowed", rowcount=n,
                         change_ids=[change_id], client=self.client, db=self.db_label)
        return Result("ok", f"undid {ch['op']} on {ch['tbl']}: {n} row(s) restored", rowcount=n,
                      change_ids=[change_id])
