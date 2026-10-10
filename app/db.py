"""データベース。DATABASE_URL があれば それ（Neon の PostgreSQL）、なければ パソコンの中のファイル。"""

import os
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        data_dir = Path(os.getenv("KINTORE_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
        data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{data_dir / 'kintore.db'}"
    # Neon などが出す postgres:// / postgresql:// を、使うドライバ（psycopg）の書き方にそろえる
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


URL = database_url()
engine = create_engine(URL, connect_args={"check_same_thread": False} if URL.startswith("sqlite") else {},
                       pool_pre_ping=True)


def init_db() -> None:
    from app import models  # noqa: F401  テーブルを登録する

    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
