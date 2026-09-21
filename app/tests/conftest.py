import io

import pytest
from PIL import Image

from app import auth, authz, db, llm

ORG = "org_1"


@pytest.fixture(autouse=True)
def _no_leaked_cooldowns():
    llm.reset_cooldowns()  # 経路の一時停止（llm.py）は、テストをまたいで持ち越さない
    yield
    llm.reset_cooldowns()


@pytest.fixture(autouse=True)
def _no_live_provider_credentials(monkeypatch):
    """テストがローカルの認証情報を拾って外部 API を呼ばないようにする。

    実際の接続設定を確認するテストは、自分で ``monkeypatch.setenv`` して明示的に有効化する。
    """
    monkeypatch.delenv("ORCA_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    db.run(c, "INSERT INTO org VALUES(?,?,?,?)", (ORG, "テスト工務店", "info@example.test", db.now()))
    db.run(c, "INSERT INTO org VALUES(?,?,?,?)", ("org_2", "別組織", "x@example.test", db.now()))
    c.commit()
    yield c
    db.reset_clock()
    c.close()


def _actor(conn, org, email, role):
    mid = auth.add_member(conn, org, email, role)
    conn.commit()
    return authz.Actor(kind="member", org_id=org, member_id=mid, role=role)


@pytest.fixture
def owner(conn):
    return _actor(conn, ORG, "owner@example.test", "owner")


@pytest.fixture
def alice(conn):
    return _actor(conn, ORG, "alice@example.test", "member")


@pytest.fixture
def bob(conn):
    return _actor(conn, ORG, "bob@example.test", "member")


@pytest.fixture
def outsider(conn):
    return _actor(conn, "org_2", "eve@example.test", "owner")


@pytest.fixture
def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 120, 200)).save(out, "JPEG")
    return out.getvalue()
