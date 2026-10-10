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


def sql_literal(value) -> str:
    """SQL の中に書く 値（最初の値に使う）。"""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def add_missing_columns() -> list[str]:
    """あとから足した項目（列）を、前からあるテーブルにも足す。足した列の名前を返す。

    create_all は 新しいテーブルは作るが、前からあるテーブルに 列は足してくれないので、ここで足す。
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateColumn

    added = []
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table in SQLModel.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                definition = str(CreateColumn(column).compile(dialect=conn.dialect))
                if not column.nullable:  # 前からある行のために、決まった最初の値を入れる
                    default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
                    if default is None:
                        raise RuntimeError(f"{table.name}.{column.name} は 最初の値が決まっていないので 足せません")
                    definition += f" DEFAULT {sql_literal(default)}"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {definition}'))
                added.append(f"{table.name}.{column.name}")
    return added


def init_db() -> None:
    from app import models  # noqa: F401  テーブルを登録する

    SQLModel.metadata.create_all(engine)
    add_missing_columns()


def get_session():
    with Session(engine) as session:
        yield session
