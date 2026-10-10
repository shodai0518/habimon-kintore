"""ログイン。インターネット上に置くので、合言葉を知っている人だけが使えるようにする。

設定（環境変数）:
- APP_PASSWORD：スマホやパソコンで開くときの 合言葉
- SYNC_TOKEN：ハビットモンスターが 記録を取りに来るときの 合言葉（長い でたらめな文字）
- SECRET_KEY：ログインの印（クッキー）に サインをするための 鍵（長い でたらめな文字）
- KINTORE_DEV=1：パソコンの中で試すとき用。ログインなしで使える（インターネット上では 絶対に付けない）
"""

import hashlib
import hmac
import os
import time

from fastapi import Cookie, Header, HTTPException, Request

from app import config

COOKIE = "kintore_session"
_failures: dict[str, list[float]] = {}  # 接続元ごとの 合言葉をまちがえた時刻


def dev_mode() -> bool:
    return os.getenv("KINTORE_DEV") == "1"


def _sign(expires: int) -> str:
    key = os.getenv("SECRET_KEY", "")
    return hmac.new(key.encode(), str(expires).encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    expires = int(time.time()) + config.LOGIN_DAYS * 86400
    return f"{expires}.{_sign(expires)}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token or not os.getenv("SECRET_KEY"):
        return False
    expires, signature = token.split(".", 1)
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, _sign(int(expires)))


def check_password(request: Request, password: str) -> None:
    """合言葉を確かめる。何度もまちがえた接続元は、しばらく受けつけない。"""
    expected = os.getenv("APP_PASSWORD")
    if not expected or not os.getenv("SECRET_KEY"):
        raise HTTPException(503, "合言葉がまだ設定されていません（APP_PASSWORD と SECRET_KEY）")
    client = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")
    recent = [t for t in _failures.get(client, []) if t > time.time() - config.LOGIN_LOCK_MINUTES * 60]
    if len(recent) >= config.LOGIN_MAX_FAILURES:
        raise HTTPException(429, f"まちがいが続いたので、{config.LOGIN_LOCK_MINUTES}分ほど待ってから試してね")
    if not hmac.compare_digest(password.encode(), expected.encode()):
        _failures[client] = recent + [time.time()]
        raise HTTPException(401, "合言葉がちがいます")
    _failures.pop(client, None)


def require_login(kintore_session: str | None = Cookie(default=None)) -> None:
    """画面からの操作：ログインしているか。"""
    if dev_mode() or valid_token(kintore_session):
        return
    raise HTTPException(401, "ログインしてね")


def require_sync(authorization: str | None = Header(default=None)) -> None:
    """ハビットモンスターからの取りこみ：合言葉（SYNC_TOKEN）が合っているか。"""
    expected = os.getenv("SYNC_TOKEN")
    if dev_mode() and not expected:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "取りこみ用の合言葉がちがいます")
