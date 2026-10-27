import os

import psycopg
import pytest

from pgbrake.classify import Kind, classify
from pgbrake.engine import Brake, Policy
from pgbrake.journal import Journal

DSN = os.environ.get("PGBRAKE_TEST_DSN", "host=/tmp port=5544 user=postgres dbname=demo")


def test_classify():
    k = lambda s: [x.kind for x in classify(s)]
    assert k("select 1") == [Kind.READ]
    assert k("delete from t where id=1") == [Kind.WRITE]
    assert k("delete from t") == [Kind.BULK_WRITE]
    assert k("update t set a=1") == [Kind.BULK_WRITE]
    assert k("drop table t") == [Kind.DDL]
    assert k("truncate t") == [Kind.BULK_WRITE]
    assert k("alter system set x=1") == [Kind.BLOCKED]
    assert k("copy t to program 'rm -rf /'") == [Kind.BLOCKED]
    assert k("with d as (delete from t returning *) select * from d") == [Kind.WRITE]
    assert k("begin") == [Kind.BLOCKED]


@pytest.fixture
def brake(tmp_path):
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("drop table if exists users; create table users(id serial primary key, email text, plan text)")
        c.execute("insert into users(email, plan) select 'u'||g||'@x.com', 'free' from generate_series(1,300) g")
    return Brake(DSN, journal=Journal(tmp_path / "j.db"), policy=Policy(max_rows=100))


def count(where="true"):
    with psycopg.connect(DSN) as c:
        return c.execute(f"select count(*) from users where {where}").fetchone()[0]


def test_read(brake):
    r = brake.run("select count(*) as n from users")
    assert r.status == "ok" and r.rows[0]["n"] == 300


def test_delete_and_undo(brake):
    r = brake.run("delete from users where id <= 10")
    assert r.status == "ok" and r.rowcount == 10 and count() == 290
    u = brake.undo(r.change_ids[0])
    assert u.status == "ok" and count() == 300


def test_update_and_undo_with_alias(brake):
    r = brake.run("update users as x set plan='pro' where x.id between 1 and 5")
    assert r.status == "ok" and count("plan='pro'") == 5
    assert brake.undo(r.change_ids[0]).status == "ok"
    assert count("plan='pro'") == 0


def test_insert_and_undo(brake):
    r = brake.run("insert into users(email, plan) values ('a@b.c','free'), ('d@e.f','free')")
    assert r.status == "ok" and count() == 302
    assert brake.undo(r.change_ids[0]).status == "ok" and count() == 300


def test_row_limit_holds_then_human_approves(brake):
    r = brake.run("delete from users where id <= 200")
    assert r.status == "needs_approval" and count() == 300
    again = brake.run("delete from users where id <= 200", approval_id=r.pending_id)
    assert again.status == "needs_approval"           # agent cannot self-approve
    brake.journal.decide(r.pending_id, "approved")     # human: `pgbrake approve <id>`
    ok = brake.run("delete from users where id <= 200", approval_id=r.pending_id)
    assert ok.status == "ok" and count() == 100
    assert brake.run("delete from users where id <= 200", approval_id=r.pending_id).status != "ok"  # one-shot
    assert brake.undo(ok.change_ids[0]).status == "ok" and count() == 300


def test_approval_bound_to_exact_sql(brake):
    r = brake.run("drop table users")
    brake.journal.decide(r.pending_id, "approved")
    assert brake.run("drop table users cascade", approval_id=r.pending_id).status == "denied"


def test_bulk_and_ddl_held(brake):
    assert brake.run("delete from users").status == "needs_approval"
    assert brake.run("drop table users").status == "needs_approval"
    assert count() == 300


def test_blocked(brake):
    assert brake.run("alter system set work_mem='1GB'").status == "denied"


def test_undo_conflict_detection(brake):
    r = brake.run("update users set plan='pro' where id=1")
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("delete from users where id=1")
    assert brake.undo(r.change_ids[0]).status == "error"


def test_multi_statement_atomic(brake):
    r = brake.run("delete from users where id=1; delete from nope where id=1")
    assert r.status == "error" and count() == 300


def test_audit_log(brake):
    brake.run("select 1")
    brake.run("delete from users")
    decisions = [a["decision"] for a in brake.journal.audit()]
    assert "needs_approval" in decisions and "allowed" in decisions
