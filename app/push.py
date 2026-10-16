"""ロック画面への通知（Web Push）。休憩が終わる時刻に、サーバーから スマホへ送る。

スマホの画面を閉じると アプリは止まるので、サーバーが時間を数えて 送る。
iPhone は、ホーム画面に追加したアプリで「通知を許可」したときだけ 受け取れる。

設定（Render の環境変数）:
- VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY：通知を送るための鍵の組（scripts/make_vapid_keys.py で作る）
- VAPID_SUBJECT：送り主の連絡先（なくてもよい。なければ このアプリのURL）
"""

import json
import logging
import os
import random
import threading
from urllib.parse import urlparse

from sqlmodel import Session

from app.models import AppSetting

logger = logging.getLogger(__name__)

SUBSCRIPTIONS_KEY = "push_subscriptions"  # 通知の送り先（スマホごとに1つ）
MAX_SUBSCRIPTIONS = 5
GRACE_SECONDS = 2  # 画面を開いているときは アプリが自分で鳴らして 取り消してくるので、少しだけ待つ
# 送り先として認める場所（Apple・Google・Mozilla・Microsoft の通知サーバー）。それ以外には 送らない
PUSH_HOSTS = (".push.apple.com", "fcm.googleapis.com", ".push.services.mozilla.com", ".notify.windows.com")

_timer: threading.Timer | None = None
_lock = threading.Lock()


# 相棒のことば（{ex} は種目の名前）。卵のときは しゃべらない
PARTNER_LINES = {
    "rest": [
        "休憩おわり！{ex}、次のセットいこう！",
        "そろそろ次のセットだよ！{ex}、がんばって！",
        "時間だよ！{ex}の次のセット、見てるからね！",
        "休憩おしまい！もうひとふんばり！",
        "よし、次いこう！{ex}、いっしょにがんばろう！",
    ],
    "test": ["通知のテストだよ。ちゃんと届いてる？"],
}
EGG_LINES = {
    "rest": "卵がゆれている…休憩おわりの合図みたい！（{ex}）",
    "test": "卵がころころゆれた。通知は ちゃんと届いているみたい！",
}


def partner_message(partner: dict | None, kind: str, exercise: str = "", rng=random) -> dict:
    """通知の中身（title・body）。相棒の名前で、相棒が話しかける形にする。相棒がまだ届いていなければ ふつうの文。"""
    ex = exercise or "筋トレ"
    if partner is None:
        return {"title": "休憩おわり" if kind == "rest" else "ハビモン筋トレ",
                "body": f"{ex}：次のセットへ！" if kind == "rest" else "通知のテストだよ。届いているね！"}
    if partner.get("motion") == "egg":
        return {"title": "卵", "body": EGG_LINES[kind].format(ex=ex)}
    lines = PARTNER_LINES[kind]
    if not exercise:  # 種目が分からないときは、種目の名前が入らない文だけ
        lines = [line for line in lines if "{ex}" not in line] or lines
    return {"title": partner["name"], "body": rng.choice(lines).format(ex=ex)}


def public_key() -> str | None:
    return os.getenv("VAPID_PUBLIC_KEY") or None


def configured() -> bool:
    return bool(public_key() and os.getenv("VAPID_PRIVATE_KEY"))


def subject() -> str:
    return os.getenv("VAPID_SUBJECT") or os.getenv("RENDER_EXTERNAL_URL") or "https://habimon-kintore.onrender.com"


def allowed_endpoint(endpoint: str) -> bool:
    url = urlparse(endpoint)
    host = url.hostname or ""
    return url.scheme == "https" and any(host == h.lstrip(".") or host.endswith(h) for h in PUSH_HOSTS)


def subscriptions(session: Session) -> list[dict]:
    setting = session.get(AppSetting, SUBSCRIPTIONS_KEY)
    return json.loads(setting.value) if setting else []


def _save(session: Session, items: list[dict]) -> None:
    value = json.dumps(items)
    setting = session.get(AppSetting, SUBSCRIPTIONS_KEY)
    if setting is None:
        session.add(AppSetting(key=SUBSCRIPTIONS_KEY, value=value))
    else:
        setting.value = value
    session.commit()


def add(session: Session, subscription: dict) -> None:
    """送り先を覚える（同じスマホなら 置きかえる。多すぎたら 古いものから忘れる）。"""
    items = [s for s in subscriptions(session) if s["endpoint"] != subscription["endpoint"]]
    _save(session, (items + [subscription])[-MAX_SUBSCRIPTIONS:])


def remove(session: Session, endpoint: str) -> None:
    _save(session, [s for s in subscriptions(session) if s["endpoint"] != endpoint])


def _send(subscription: dict, payload: dict) -> None:
    from pywebpush import webpush

    webpush(
        subscription_info=subscription,
        data=json.dumps(payload, ensure_ascii=False),
        vapid_private_key=os.getenv("VAPID_PRIVATE_KEY"),
        vapid_claims={"sub": subject()},
        ttl=60,  # 1分たっても届かなければ もう送らない（休憩の知らせは 遅れたら意味がない）
        headers={"Urgency": "high"},
        timeout=10,
    )


def send_all(session: Session, payload: dict, send=None) -> int:
    """覚えている送り先 ぜんぶに送る。もう使われていない送り先は 忘れる。送れた数を返す。"""
    sent, gone = 0, []
    for sub in subscriptions(session):
        try:
            (send or _send)(sub, payload)
            sent += 1
        except Exception as err:
            status = getattr(getattr(err, "response", None), "status_code", None)
            if status in (404, 410):  # スマホ側で 通知をやめた
                gone.append(sub["endpoint"])
            else:
                logger.warning("通知を送れませんでした（%s）", status or type(err).__name__)
    for endpoint in gone:
        remove(session, endpoint)
    return sent


def _fire(payload: dict) -> None:
    from app.db import engine

    try:
        with Session(engine) as session:
            send_all(session, payload)
    except Exception:
        logger.exception("通知の送信でエラーが起きました")


def schedule(seconds: float, payload: dict) -> None:
    """seconds 秒後に 通知を送る（前の予約は 取り消す。予約は1つだけ）。"""
    global _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(max(0.0, seconds) + GRACE_SECONDS, _fire, [payload])
        _timer.daemon = True
        _timer.start()


def cancel() -> bool:
    """予約を取り消す。予約があったら True。"""
    global _timer
    with _lock:
        had = _timer is not None and _timer.is_alive()
        if _timer is not None:
            _timer.cancel()
        _timer = None
        return had
