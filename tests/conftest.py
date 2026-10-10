import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("KINTORE_DATA_DIR", str(Path(__file__).resolve().parent / "_data"))

from app import auth, clock, main  # noqa: E402
from app.db import get_session  # noqa: E402

NOW = datetime(2026, 10, 10, 20, 0)


@pytest.fixture
def engine(monkeypatch):
    # 本物のDBを使わず、テストごとに メモリ上の 空のDBを使う
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        main.ensure_presets(session)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    for name in ("KINTORE_DEV", "APP_PASSWORD", "SECRET_KEY", "SYNC_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    auth._failures.clear()
    return engine


@pytest.fixture
def app_client(engine):
    """ログインしていない状態のクライアント。"""
    def override():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[get_session] = override
    yield TestClient(main.app)  # with を使わないので、本物のDBを作る起動処理は動かない
    main.app.dependency_overrides.clear()


@pytest.fixture
def client(app_client, monkeypatch):
    """ログインなしで使える（パソコンで試すとき用の設定）クライアント。"""
    monkeypatch.setenv("KINTORE_DEV", "1")
    return app_client
