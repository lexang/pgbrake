import io, json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as s
import base64
from pgbrake import license as lic, guard


def _pair():
    k = Ed25519PrivateKey.generate()
    pub = base64.urlsafe_b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    return k, pub


def test_sign_verify_and_tamper():
    k, pub = _pair()
    key = lic.sign({"email": "a@b.c", "order": "o1", "plan": "pro"}, k)
    r = lic.verify(key, pub)
    assert r.valid and r.email == "a@b.c"
    p, body, sig = key.split(".")
    forged = lic._b64e(json.dumps({"email": "x@y.z", "plan": "pro"}).encode())
    assert not lic.verify(f"{p}.{forged}.{sig}", pub).valid
    assert not lic.verify("garbage", pub).valid
    assert not lic.verify(key).valid  # wrong (production) public key


def test_activate_rejects_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("PGBRAKE_HOME", str(tmp_path))
    monkeypatch.delenv("PGBRAKE_LICENSE", raising=False)
    assert not lic.activate("PGB1.abc.def").valid
    assert not (tmp_path / "license").exists()
    assert not lic.is_pro()


import pytest

@pytest.mark.parametrize("cmd", [
    "npx prisma migrate reset --force",
    "npx prisma db push --accept-data-loss",
    "bin/rails db:drop",
    "python manage.py flush --noinput",
    "python manage.py migrate shop zero",
    "alembic downgrade base",
    "supabase db reset",
    "psql $DATABASE_URL -c 'DROP TABLE users'",
    "npx drizzle-kit drop",
    "dropdb app_dev",
])
def test_destructive_commands_match(cmd):
    assert guard.match(cmd)


@pytest.mark.parametrize("cmd", [
    "npx prisma migrate dev", "alembic upgrade head", "bin/rails db:migrate",
    "python manage.py migrate", "psql -c 'select 1'", "ls -la", "git reset --hard",
])
def test_safe_commands_pass(cmd):
    assert guard.match(cmd) is None


def test_hook_outputs(monkeypatch):
    out = guard.decide("npx prisma migrate reset --force", pro=True, dsn=None)
    h = out["hookSpecificOutput"]
    assert h["permissionDecision"] == "ask" and "prisma" in h["permissionDecisionReason"]
    assert "systemMessage" in guard.decide("npx prisma migrate reset --force", pro=False)
    assert guard.decide("npm test", pro=True) is None
    monkeypatch.setattr(guard.lic, "is_pro", lambda: True)
    buf = io.StringIO()
    guard.hook_main(io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "bin/rails db:reset"}})), buf)
    assert json.loads(buf.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"
    buf = io.StringIO()
    guard.hook_main(io.StringIO(json.dumps({"tool_name": "Edit", "tool_input": {}})), buf)
    assert buf.getvalue() == ""


def test_install_idempotent(tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"permissions": {"allow": []}}))
    guard.install(p); guard.install(p)
    d = json.loads(p.read_text())
    assert d["permissions"] == {"allow": []}
    assert len(d["hooks"]["PreToolUse"]) == 1
