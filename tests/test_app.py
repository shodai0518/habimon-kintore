from datetime import timedelta

from conftest import NOW


def exercise_id(client, name):
    return next(e["id"] for e in client.get("/api/exercises").json()["exercises"] if e["name"] == name)


def record(client, name="ベンチプレス", sets=((90, 5),), minutes=60, weight=70, start=None):
    """あとから記録する形（終了が 10分前）。セットを追加した時刻は 筋トレの時間の外なので、決まった長さで数える。"""
    start = start or NOW - timedelta(minutes=minutes + 10)
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
    res = client.post(f"/api/workouts/{wid}/finish", json={"ended_at": (NOW - timedelta(minutes=10)).isoformat()}).json()
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
    client.post(f"/api/workouts/{wid}/finish", json={"ended_at": (NOW - timedelta(minutes=10)).isoformat()})
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


def test_rest_seconds(client):
    by_name = {e["name"]: e for e in client.get("/api/exercises").json()["exercises"]}
    assert by_name["ベンチプレス"]["rest_seconds"] == 180  # ビッグ3
    assert by_name["懸垂"]["rest_seconds"] == 120 and by_name["アームカール"]["rest_seconds"] == 90
    assert by_name["ランニング"]["rest_seconds"] is None  # 有酸素は タイマーなし
    curl = by_name["アームカール"]["id"]
    assert client.put(f"/api/exercises/{curl}/rest", json={"seconds": 105}).json() == {"rest_seconds": 105}
    by_name = {e["name"]: e for e in client.get("/api/exercises").json()["exercises"]}
    assert by_name["アームカール"]["rest_seconds"] == 105  # 変えた長さを覚えている
    assert client.put(f"/api/exercises/{curl}/rest", json={"seconds": 5}).status_code == 422
    assert client.put(f"/api/exercises/{by_name['ランニング']['id']}/rest", json={"seconds": 60}).status_code == 404


def test_add_missing_columns(tmp_path, monkeypatch):
    """前からあるテーブルに、あとから足した列が 自動で足される（データは消えない）。"""
    from sqlalchemy import inspect, text
    from sqlmodel import create_engine

    from app import db

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:  # rest_seconds と warmup_sets が まだない、古い形のテーブル
        conn.execute(text("CREATE TABLE exercise (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, part VARCHAR NOT NULL, mets FLOAT, mets_estimated BOOLEAN NOT NULL, preset BOOLEAN NOT NULL, created_at DATETIME NOT NULL)"))
        conn.execute(text("INSERT INTO exercise VALUES (1, 'ベンチプレス', 'chest', 6.0, 1, 1, '2026-10-01 00:00:00')"))
    monkeypatch.setattr(db, "engine", engine)
    assert sorted(db.add_missing_columns()) == ["exercise.rest_seconds", "exercise.warmup_sets"]
    assert db.add_missing_columns() == []  # 2回目は 何もしない
    with engine.begin() as conn:
        assert {"rest_seconds", "warmup_sets"} <= {c["name"] for c in inspect(conn).get_columns("exercise")}
        assert conn.execute(text("SELECT name, warmup_sets, rest_seconds FROM exercise")).one() == ("ベンチプレス", 0, None)


def test_live_workout_kcal_uses_actual_rest(client, monkeypatch):
    """今やっている筋トレ：セットを追加した時刻の間（休憩＋動いている時間）で カロリーを数える。"""
    from app import clock

    at = {"now": NOW}
    monkeypatch.setattr(clock, "now", lambda: at["now"])
    bench = exercise_id(client, "ベンチプレス")
    w = client.post("/api/workouts/start", json={"body_weight": 70}).json()
    client.put(f"/api/workouts/{w['id']}/warmup", json={"exercise_id": bench, "sets": 2})
    for minute in (7, 10, 30):  # 7分後（アップ込み）・10分後・30分後（長く空いた）
        at["now"] = NOW + timedelta(minutes=minute)
        view = client.post(f"/api/workouts/{w['id']}/sets", json={"exercise_id": bench, "weight": 90, "reps": 5}).json()
    # 7分（上限は 5＋アップ3分＝8分）＋ 3分 ＋ 5分（20分あいたが 上限まで）= 15分
    assert view["kcal"] == round(6.0 * 70 * 15 / 60 * 1.05)
    at["now"] = NOW + timedelta(minutes=40)
    res = client.post(f"/api/workouts/{w['id']}/finish", json={}).json()
    assert res["workout"]["kcal"] == round(6.0 * 70 * 15 / 60 * 1.05)


def test_add_missing_bool_column(tmp_path, monkeypatch):
    """あとから足した「お気に入り」の列（最初は False）が、前からあるテーブルに足される。"""
    from sqlalchemy import text
    from sqlmodel import create_engine

    from app import db

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE menuitem (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, kcal FLOAT NOT NULL, protein FLOAT NOT NULL, fat FLOAT NOT NULL, carbs FLOAT NOT NULL, items VARCHAR NOT NULL, use_count INTEGER NOT NULL, last_used DATETIME, created_at DATETIME NOT NULL)"))
        conn.execute(text("INSERT INTO menuitem VALUES (1, 'オイコス', 92, 12, 0, 5.4, '[]', 3, NULL, '2026-10-11 00:00:00')"))
    monkeypatch.setattr(db, "engine", engine)
    assert "menuitem.favorite" in db.add_missing_columns()
    with engine.begin() as conn:
        assert conn.execute(text("SELECT name, use_count, favorite FROM menuitem")).one() == ("オイコス", 3, 0)


def test_partner_from_habimon(client):
    """ハビットモンスターが送ってきた相棒を 出す（画像は 別に取りにいく）。"""
    import base64

    assert client.get("/api/partner").json() == {"partner": None}
    assert client.get("/api/partner/image").status_code == 404

    image = base64.b64encode(b"RIFF....WEBP-no-nakami").decode()
    assert client.put("/api/sync/partner", json={"name": "キノバット", "rank": "E", "level": 6, "image": image}).json()["ok"]
    partner = client.get("/api/partner").json()["partner"]
    assert (partner["name"], partner["rank"], partner["level"]) == ("キノバット", "E", 6)
    res = client.get(partner["image_url"])
    assert res.content == b"RIFF....WEBP-no-nakami" and res.headers["content-type"] == "image/webp"
    assert "immutable" in res.headers["cache-control"]

    # レベルが上がった（画像は同じ）→ 画像の番号は そのまま
    client.put("/api/sync/partner", json={"name": "キノバット", "rank": "E", "level": 7, "image": image})
    again = client.get("/api/partner").json()["partner"]
    assert again["level"] == 7 and again["image_url"] == partner["image_url"]

    # 画像がない（卵など）
    client.put("/api/sync/partner", json={"name": "卵", "rank": "D", "level": 1, "image": None})
    assert client.get("/api/partner").json()["partner"] == {"name": "卵", "rank": "D", "level": 1, "motion": "hop", "image_url": None}

    # なでたときの動きも 届く
    client.put("/api/sync/partner", json={"name": "キノバット", "rank": "E", "level": 7, "motion": "float", "image": image})
    assert client.get("/api/partner").json()["partner"]["motion"] == "float"
    assert client.put("/api/sync/partner", json={"name": "x", "rank": "E", "level": 1, "motion": "dance"}).status_code == 422

    assert client.put("/api/sync/partner", json={"name": "x", "rank": "E", "level": 1, "image": "これは画像じゃない"}).status_code == 400
    assert client.put("/api/sync/partner", json={"name": "", "rank": "E", "level": 1}).status_code == 422
