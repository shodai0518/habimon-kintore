"""ロック画面への通知（Web Push）。本物の通知サーバーには 送らない。"""

import pytest

from app import push

SUB = {"endpoint": "https://web.push.apple.com/abc123", "keys": {"p256dh": "p" * 87, "auth": "a" * 22}}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "B" + "x" * 86)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "k" * 43)


@pytest.fixture
def booked(monkeypatch):
    """予約の中身をためる（本物のタイマーは 動かさない）。"""
    calls = []
    monkeypatch.setattr(push, "schedule", lambda seconds, payload: calls.append((seconds, payload)))
    return calls


def test_push_off_until_keys_are_set(client):
    assert client.get("/api/push").json() == {"available": False, "public_key": None, "count": 0}
    assert client.post("/api/push/subscribe", json=SUB).status_code == 503
    assert client.post("/api/rest/notify", json={"seconds": 90, "name": "ベンチプレス"}).json() == {"scheduled": False}


def test_subscribe_and_book_rest_notice(client, keys, booked):
    assert client.get("/api/push").json()["available"]
    # まだ どのスマホも 登録していない → 予約しない
    assert client.post("/api/rest/notify", json={"seconds": 90, "name": "ベンチプレス"}).json() == {"scheduled": False}
    assert client.post("/api/push/test").status_code == 400

    assert client.post("/api/push/subscribe", json=SUB).json()["ok"]
    assert client.post("/api/push/subscribe", json=SUB).json()["ok"]  # 同じスマホは 1つにまとめる
    assert client.get("/api/push").json()["count"] == 1

    assert client.post("/api/rest/notify", json={"seconds": 90, "name": "ベンチプレス"}).json() == {"scheduled": True}
    seconds, payload = booked[0]
    # 相棒がまだ届いていないときは ふつうの文
    assert seconds == 90 and payload == {"title": "休憩おわり", "body": "ベンチプレス：次のセットへ！", "tag": "rest"}
    assert client.post("/api/push/test").json()["seconds"] == 5 and booked[1][1]["tag"] == "test"

    assert client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]}).json()["ok"]
    assert client.get("/api/push").json()["count"] == 0
    assert client.post("/api/rest/notify", json={"seconds": 99999}).status_code == 422


def test_only_real_push_servers_are_accepted(client, keys):
    """通知サーバー以外の場所には 送らない。"""
    for endpoint in ("https://evil.example/hook", "http://web.push.apple.com/abc", "https://push.apple.com.evil.example/x"):
        assert client.post("/api/push/subscribe", json={**SUB, "endpoint": endpoint}).status_code == 400
    assert push.allowed_endpoint("https://fcm.googleapis.com/fcm/send/abc")
    assert push.allowed_endpoint("https://updates.push.services.mozilla.com/wpush/v2/abc")


def test_send_all_forgets_dead_subscriptions(client, keys, engine):
    from sqlmodel import Session

    client.post("/api/push/subscribe", json=SUB)
    client.post("/api/push/subscribe", json={**SUB, "endpoint": "https://web.push.apple.com/old"})

    class Gone(Exception):
        class response:
            status_code = 410

    sent = []

    def send(sub, payload):
        if sub["endpoint"].endswith("/old"):
            raise Gone()
        sent.append((sub["endpoint"], payload))

    with Session(engine) as session:
        assert push.send_all(session, {"title": "休憩おわり"}, send=send) == 1
        assert sent == [(SUB["endpoint"], {"title": "休憩おわり"})]
        assert [s["endpoint"] for s in push.subscriptions(session)] == [SUB["endpoint"]]  # 使えない送り先は 忘れる


def test_schedule_replaces_and_cancel(monkeypatch):
    made = []

    class FakeTimer:
        def __init__(self, seconds, fn, args):
            self.seconds, self.cancelled, self.daemon = seconds, False, False
            made.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return not self.cancelled

    monkeypatch.setattr(push.threading, "Timer", FakeTimer)
    monkeypatch.setattr(push, "_timer", None)
    push.schedule(90, {"title": "a"})
    push.schedule(120, {"title": "b"})  # 長さを変えた → 前の予約は 取り消す
    assert made[0].cancelled and not made[1].cancelled
    assert made[1].seconds == 120 + push.GRACE_SECONDS
    assert push.cancel() is True and made[1].cancelled
    assert push.cancel() is False


def test_generated_keys_work_with_the_push_library():
    """鍵を作るスクリプトの鍵が、通知のライブラリで そのまま使える形になっている。"""
    import base64
    import sys
    from pathlib import Path

    from py_vapid import Vapid

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from make_vapid_keys import make_keys

    public, private = make_keys()
    raw = base64.urlsafe_b64decode(public + "=" * (-len(public) % 4))
    assert len(raw) == 65 and raw[0] == 4  # スマホに渡す形（圧縮していない点）
    headers = Vapid.from_string(private).sign({"sub": "https://habimon-kintore.onrender.com", "aud": "https://web.push.apple.com"})
    assert headers["Authorization"].startswith("vapid t=") and public in headers["Authorization"]



def test_notice_is_spoken_by_partner(client, keys, booked):
    """相棒が届いていれば、相棒の名前で 話しかける。"""
    client.post("/api/push/subscribe", json=SUB)
    client.put("/api/sync/partner", json={"name": "キノバット", "rank": "E", "level": 6, "motion": "sway"})
    client.post("/api/rest/notify", json={"seconds": 120, "name": "ベンチプレス"})
    payload = booked[-1][1]
    assert payload["title"] == "キノバット" and payload["tag"] == "rest"
    assert payload["body"] in [line.format(ex="ベンチプレス") for line in push.PARTNER_LINES["rest"]]

    client.post("/api/push/test")
    assert booked[-1][1]["title"] == "キノバット" and booked[-1][1]["tag"] == "test"

    client.put("/api/sync/partner", json={"name": "卵", "rank": "D", "level": 1, "motion": "egg"})
    client.post("/api/rest/notify", json={"seconds": 60, "name": "スクワット"})
    assert booked[-1][1] == {"title": "卵", "body": "卵がゆれている…休憩おわりの合図みたい！（スクワット）", "tag": "rest"}


def test_lines_without_exercise_name():
    import random

    partner = {"name": "キノバット", "motion": "hop"}
    for seed in range(20):
        body = push.partner_message(partner, "rest", "", rng=random.Random(seed))["body"]
        assert "{ex}" not in body and "筋トレ" not in body



def test_notice_uses_partner_own_lines(client, keys, booked):
    """キャラごとのセリフが届いていれば、休憩おわりは それを使う。"""
    client.post("/api/push/subscribe", json=SUB)
    client.put("/api/sync/partner", json={"name": "チクタク", "rank": "C", "level": 22, "motion": "hop",
                                          "lines": ["チクタク…{ex}の時間だ！"]})
    client.post("/api/rest/notify", json={"seconds": 90, "name": "ベンチプレス"})
    assert booked[-1][1] == {"title": "チクタク", "body": "チクタク…ベンチプレスの時間だ！", "tag": "rest"}
    client.post("/api/push/test")  # テストは いつもの文
    assert booked[-1][1]["body"] in push.PARTNER_LINES["test"]
    assert client.put("/api/sync/partner", json={"name": "x", "rank": "E", "level": 1, "lines": ["a"] * 9}).status_code == 422
