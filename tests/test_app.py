from datetime import timedelta

from conftest import NOW


def exercise_id(client, name):
    return next(e["id"] for e in client.get("/api/exercises").json()["exercises"] if e["name"] == name)


def record(client, name="ベンチプレス", sets=((90, 5),), minutes=60, weight=70, start=None):
    start = start or NOW - timedelta(minutes=minutes)
    w = client.post("/api/workouts/start", json={"body_weight": weight, "started_at": start.isoformat(),
                                                  "planned_end": (start + timedelta(minutes=minutes)).isoformat()}).json()
    ex = exercise_id(client, name)
    for kg, reps in sets:
        client.post(f"/api/workouts/{w['id']}/sets", json={"exercise_id": ex, "weight": kg, "reps": reps})
    return w["id"], ex


# ---- ログイン ----
def test_login_required(app_client, monkeypatch):
    assert app_client.get("/api/workouts").status_code == 401
    assert app_client.get("/api/sync/workouts").status_code == 401
    assert app_client.post("/api/login", json={"password": "x"}).status_code == 503  # 合言葉が まだ設定されていない
    monkeypatch.setenv("APP_PASSWORD", "ひみつ")
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    assert app_client.post("/api/login", json={"password": "ちがう"}).status_code == 401
    assert app_client.post("/api/login", json={"password": "ひみつ"}).status_code == 200
    assert app_client.get("/api/workouts").status_code == 200  # クッキーで 入れる
    app_client.post("/api/logout")
    assert app_client.get("/api/workouts").status_code == 401


def test_login_lock_and_forged_cookie(app_client, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "ひみつ")
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    app_client.cookies.set("kintore_session", "9999999999.deadbeef")  # にせもの
    assert app_client.get("/api/workouts").status_code == 401
    for _ in range(10):
        app_client.post("/api/login", json={"password": "ちがう"})
    assert app_client.post("/api/login", json={"password": "ひみつ"}).status_code == 429  # まちがいが続くと 待たされる


def test_sync_token(app_client, monkeypatch):
    monkeypatch.setenv("SYNC_TOKEN", "t" * 40)
    assert app_client.get("/api/sync/workouts", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert app_client.get("/api/sync/workouts", headers={"Authorization": "Bearer " + "t" * 40}).status_code == 200


# ---- 記録 ----
def test_record_flow_with_warmup_and_calories(client):
    wid, bench = record(client, sets=((90, 5), (90, 5)))
    view = client.put(f"/api/workouts/{wid}/warmup", json={"exercise_id": bench, "sets": 2}).json()
    assert view["exercises"][0]["warmup"] == 2
    # ベンチ（ビッグ3）2セット × 2.5分 ＋ アップ2セット × 1.5分 = 8分
    assert view["kcal"] == round(6.0 * 70 * 8 / 60 * 1.05)
    res = client.post(f"/api/workouts/{wid}/finish", json={"ended_at": NOW.isoformat()}).json()
    assert res["workout"]["status"] == "done" and res["workout"]["minutes"] == 60
    data = client.get("/api/workouts").json()
    assert data["current"] is None and data["history"][0]["id"] == wid and data["last_weight"] == 70
    ex = next(e for e in client.get("/api/exercises").json()["exercises"] if e["id"] == bench)
    assert ex["warmup_sets"] == 2 and ex["last"]["sets"] == ["90kg×5回", "90kg×5回"] and ex["best"] == "90kg×5回"


def test_pr_and_rules(client):
    wid, _ = record(client, sets=((80, 5),), start=NOW - timedelta(days=2))
    client.post(f"/api/workouts/{wid}/finish", json={"ended_at": (NOW - timedelta(days=2) + timedelta(minutes=30)).isoformat()})
    wid, _ = record(client, sets=((90, 5),))
    res = client.post(f"/api/workouts/{wid}/finish", json={"ended_at": NOW.isoformat()}).json()
    assert any("自己ベスト更新" in m for m in res["messages"])
    assert res["workout"]["exercises"][0]["sets"][0]["is_pr"]
    # 記録中は1つだけ・セットなしでは 終われない・やめると 消える
    w = client.post("/api/workouts/start", json={}).json()
    assert client.post("/api/workouts/start", json={}).status_code == 409
    assert client.post(f"/api/workouts/{w['id']}/finish", json={}).status_code == 400
    assert client.delete(f"/api/workouts/{w['id']}").json()["ok"]
    assert client.post("/api/workouts/start", json={"started_at": (NOW - timedelta(days=30)).isoformat()}).status_code == 400


def test_new_exercise_and_cardio(client):
    ex = client.post("/api/exercises", json={"name": "ケトルベルスイング", "part": "legs"}).json()
    assert client.post("/api/exercises", json={"name": "ケトルベルスイング", "part": "legs"}).status_code == 409
    run = exercise_id(client, "ランニング")
    w = client.post("/api/workouts/start", json={"body_weight": 60, "started_at": (NOW - timedelta(minutes=40)).isoformat()}).json()
    view = client.post(f"/api/workouts/{w['id']}/sets", json={"exercise_id": run, "duration_min": 30, "distance_km": 4.8}).json()
    assert view["exercises"][0]["sets"][0]["label"] == "30分・4.8km" and view["kcal"] > 200
    assert client.post(f"/api/workouts/{w['id']}/sets", json={"exercise_id": ex["id"]}).status_code == 400  # 回数がない


# ---- ハビモンとの やりとり ----
def test_sync_export(client):
    wid, bench = record(client)
    client.put(f"/api/workouts/{wid}/warmup", json={"exercise_id": bench, "sets": 1})
    assert client.get("/api/sync/workouts").json()["workouts"] == []  # 記録中のものは 出さない
    client.post(f"/api/workouts/{wid}/finish", json={"ended_at": NOW.isoformat()})
    client.post("/api/exercises", json={"name": "ケトルベルスイング", "part": "legs"})
    data = client.get("/api/sync/workouts?since=2026-10-01").json()
    w = data["workouts"][0]
    assert w["id"] == wid and w["minutes"] == 60 and w["warmups"] == {"ベンチプレス": 1}
    assert w["sets"][0]["exercise"] == "ベンチプレス" and w["sets"][0]["weight"] == 90
    new = next(e for e in data["exercises"] if e["name"] == "ケトルベルスイング")
    assert new["mets_estimated"] is False  # 仮の強さ → ハビモンが AIで決めて 書きもどす
    assert client.put("/api/sync/exercises/mets", json={"mets": {"ケトルベルスイング": 9.8}}).json()["updated"] == 1
    data = client.get("/api/sync/workouts").json()
    assert next(e for e in data["exercises"] if e["name"] == "ケトルベルスイング")["mets_estimated"] is True
    assert client.get("/api/sync/workouts?since=2026-11-01").json()["workouts"] == []


def test_sync_import_is_idempotent(client):
    body = {
        "exercises": [{"name": "大文字登山", "part": "cardio", "mets": 7.0}, {"name": "ベンチプレス", "part": "chest", "mets": 6.0, "warmup_sets": 2}],
        "workouts": [{
            "source_id": 5, "day": "2026-10-07", "started_at": "2026-10-07T08:30:00", "ended_at": "2026-10-07T09:34:00",
            "minutes": 64, "body_weight": 72.0, "warmups": {"ベンチプレス": 2},
            "sets": [{"exercise": "ベンチプレス", "weight": 90, "reps": 3, "is_pr": True},
                     {"exercise": "大文字登山", "duration_min": 40}],
        }],
    }
    first = client.post("/api/sync/import", json=body).json()
    second = client.post("/api/sync/import", json=body).json()
    assert first["workouts"] == second["workouts"]  # 2回送っても 増えない
    history = client.get("/api/workouts").json()["history"]
    assert len(history) == 1 and history[0]["minutes"] == 64 and history[0]["exercises"][0]["warmup"] == 2
    exported = client.get("/api/sync/workouts").json()["workouts"][0]
    assert exported["source_id"] == 5
    bench = next(e for e in client.get("/api/exercises").json()["exercises"] if e["name"] == "ベンチプレス")
    assert bench["warmup_sets"] == 2


def test_japan_time_is_available():
    """時間帯のデータが入っていて、日本時間で動く（サーバーが UTC でも）。"""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from app import clock, config

    real = datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None)  # clock.now は テストで固定しているので 直接たしかめる
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert 8.9 < (real - utc).total_seconds() / 3600 < 9.1
    assert clock.game_day(datetime(2026, 10, 10, 3, 59)).isoformat() == "2026-10-09"


def test_sync_delete_done_workout(client):
    wid, _ = record(client)
    client.post(f"/api/workouts/{wid}/finish", json={"ended_at": NOW.isoformat()})
    assert client.delete(f"/api/sync/workouts/{wid}").json() == {"deleted": True}
    assert client.get("/api/workouts").json()["history"] == []
    assert client.get("/api/sync/workouts").json()["workouts"] == []
    assert client.delete(f"/api/sync/workouts/{wid}").json() == {"deleted": False}  # もう ない
