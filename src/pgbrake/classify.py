"""Classify SQL statements by risk using the real Postgres parser (pglast)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pglast import parse_sql
from pglast.stream import RawStream


class Kind(str, Enum):
    READ = "read"          # SELECT / SHOW / EXPLAIN without ANALYZE
    WRITE = "write"        # INSERT / UPDATE / DELETE with a WHERE clause
    BULK_WRITE = "bulk"    # UPDATE / DELETE without WHERE, TRUNCATE
    DDL = "ddl"            # CREATE / ALTER / DROP / GRANT ...
    BLOCKED = "blocked"    # things we never pass through (COPY TO PROGRAM, ALTER SYSTEM...)
    OTHER = "other"        # SET, BEGIN, unknown -> treated as needing approval


READ_NODES = {"SelectStmt", "VariableShowStmt", "ExplainStmt"}
WRITE_NODES = {"InsertStmt", "UpdateStmt", "DeleteStmt", "MergeStmt"}
BLOCKED_NODES = {"AlterSystemStmt", "CreateRoleStmt", "AlterRoleStmt", "DropRoleStmt",
                 "DropdbStmt", "CreatedbStmt", "LoadStmt", "DoStmt"}
DDL_PREFIXES = ("Create", "Alter", "Drop", "Grant", "Rename", "Comment", "Truncate",
                "Reindex", "Cluster", "Vacuum", "Refresh", "Lock")


@dataclass
class Statement:
    sql: str
    node: str
    kind: Kind
    table: str | None = None
    schema: str | None = None
    where_sql: str | None = None
    has_returning: bool = False
    alias: str | None = None
    from_sql: str | None = None      # extra FROM/USING items for join-based writes
    undo_supported: bool = True
    reasons: list[str] = field(default_factory=list)

    @property
    def qualified_table(self) -> str | None:
        if not self.table:
            return None
        return f'"{self.schema}"."{self.table}"' if self.schema else f'"{self.table}"'


def _rangevar(rv):
    if rv is None:
        return None, None
    return rv.relname, rv.schemaname


def _select_writes(stmt) -> bool:
    """SELECT ... INTO, or data-modifying CTEs inside a SELECT."""
    if getattr(stmt, "intoClause", None) is not None:
        return True
    wc = getattr(stmt, "withClause", None)
    if wc and wc.ctes:
        for cte in wc.ctes:
            if type(cte.ctequery).__name__ in WRITE_NODES:
                return True
    return False


def classify(sql: str) -> list[Statement]:
    out: list[Statement] = []
    for raw in parse_sql(sql):
        node = raw.stmt
        name = type(node).__name__
        text = RawStream()(node)
        st = Statement(sql=text, node=name, kind=Kind.OTHER)

        if name in READ_NODES:
            st.kind = Kind.READ
            if name == "SelectStmt" and _select_writes(node):
                st.kind = Kind.WRITE
                st.reasons.append("SELECT with data-modifying CTE / INTO")
            if name == "ExplainStmt":
                opts = [o.defname.lower() for o in (node.options or [])]
                if "analyze" in opts:
                    inner = classify(RawStream()(node.query))[0]
                    st.kind = inner.kind if inner.kind != Kind.READ else Kind.READ
                    st.reasons.append("EXPLAIN ANALYZE executes the statement")
        elif name in WRITE_NODES:
            st.table, st.schema = _rangevar(node.relation)
            if node.relation is not None and node.relation.alias is not None:
                st.alias = node.relation.alias.aliasname
            st.has_returning = bool(getattr(node, "returningList", None))
            if name in ("UpdateStmt", "DeleteStmt"):
                where = node.whereClause
                if where is None:
                    st.kind = Kind.BULK_WRITE
                    st.reasons.append(f"{name[:-4].upper()} without WHERE affects every row")
                else:
                    st.kind = Kind.WRITE
                    st.where_sql = RawStream()(where)
                    extra = getattr(node, "fromClause", None) or getattr(node, "usingClause", None)
                    if extra:
                        st.from_sql = ", ".join(RawStream()(x) for x in extra)
            elif name == "MergeStmt":
                st.kind = Kind.BULK_WRITE
                st.undo_supported = False
                st.reasons.append("MERGE can touch many rows and is not undoable")
            else:
                st.kind = Kind.WRITE
                occ = getattr(node, "onConflictClause", None)
                if occ is not None and int(occ.action) == 2:
                    st.undo_supported = False
                    st.reasons.append("upsert (ON CONFLICT DO UPDATE) is not undoable")
        elif name == "CopyStmt":
            if getattr(node, "is_program", False) or (not node.is_from and node.filename):
                st.kind = Kind.BLOCKED
                st.reasons.append("COPY to/from server file or program")
            else:
                st.kind = Kind.BULK_WRITE if node.is_from else Kind.READ
        elif name in BLOCKED_NODES:
            st.kind = Kind.BLOCKED
            st.reasons.append(f"{name} is never allowed through pgbrake")
        elif name == "TruncateStmt":
            st.kind = Kind.BULK_WRITE
            st.undo_supported = False
            st.reasons.append("TRUNCATE removes every row and cannot be undone by pgbrake")
        elif name.startswith(DDL_PREFIXES):
            st.kind = Kind.DDL
            st.reasons.append(f"schema change ({name})")
        elif name == "TransactionStmt":
            st.kind = Kind.BLOCKED
            st.reasons.append("pgbrake manages transactions itself; send one statement group per call")
        out.append(st)
    return out
