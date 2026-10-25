"""筋トレメニュー：読み取り・保存・筋トレ中の進み具合。"""

from app import menu_ai

ITEMS = [
    {"exercise": "スクワット", "part": "legs", "alternatives": [], "sets_min": 2, "sets_max": 2, "reps": "5〜8回", "note": ""},
    {"exercise": "ノルディック・ハムストリングス", "part": "legs", "alternatives": [], "sets_min": 2, "sets_max": 3,
     "reps": "限界回数", "note": "肉離れ予防"},
    {"exercise": "懸垂", "part": "back", "alternatives": ["ラットプルダウン"], "sets_min": 3, "sets_max": 3, "reps": "8〜12回", "note": ""},
]


def test_parse_menu_text(client, monkeypatch):
    seen = {}

    def fake(text, existing):
        seen["existing"] = dict(existing)
        return menu_ai.Plan(menus=[menu_ai.PlanMenu(name="下半身の日", items=[menu_ai.PlanItem(**ITEMS[0]), menu_ai.PlanItem(**ITEMS[1])])])

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(menu_ai, "parse", fake)
    res = client.post("/api/workout-menus/parse", json={"text": "バックスクワット 5〜8回×2セット ほか"}).json()
    assert res["menus"][0]["name"] == "下半身の日"
    assert [(i["exercise"], i["exists"]) for i in res["menus"][0]["items"]] == [("スクワット", True), ("ノルディック・ハムストリングス", False)]
    assert seen["existing"]["スクワット"] == "legs"  # 今ある種目を AIに 渡している
    assert client.get("/api/workout-menus").json()["menus"] == []  # 読み取っただけでは 保存しない


def test_save_menu_creates_missing_exercises(client):
    menu = client.post("/api/workout-menus", json={"name": "下半身の日", "items": ITEMS}).json()
    assert [i["name"] for i in menu["items"]] == ["スクワット", "ノルディック・ハムストリングス", "懸垂"]
    assert menu["items"][2]["alternatives"][0]["name"] == "ラットプルダウン"
    names = [e["name"] for e in client.get("/api/exercises").json()["exercises"]]
    assert "ノルディック・ハムストリングス" in names  # 新しい種目は 作られる
    updated = client.put(f"/api/workout-menus/{menu['id']}", json={"name": "脚の日", "items": ITEMS[:1]}).json()
    assert updated["name"] == "脚の日" and len(updated["items"]) == 1
    assert client.delete(f"/api/workout-menus/{menu['id']}").json()["ok"]
    assert client.get("/api/workout-menus").json()["menus"] == []


def test_workout_shows_menu_progress(client):
    menu = client.post("/api/workout-menus", json={"name": "上半身の日", "items": ITEMS}).json()
    w = client.post("/api/workouts/start", json={"body_weight": 70, "menu_id": menu["id"]}).json()
    assert [i["done"] for i in w["menu"]["items"]] == [0, 0, 0]
    lat = menu["items"][2]["alternatives"][0]["id"]  # 懸垂の代わりに ラットプルダウンを やった
    squat = menu["items"][0]["exercise_id"]
    client.post(f"/api/workouts/{w['id']}/sets", json={"exercise_id": squat, "weight": 80, "reps": 6})
    client.post(f"/api/workouts/{w['id']}/sets", json={"exercise_id": lat, "weight": 50, "reps": 10})
    current = client.get("/api/workouts").json()["current"]
    items = current["menu"]["items"]
    assert [i["done"] for i in items] == [1, 0, 1] and items[2]["used_id"] == lat
    # 途中で メニューを やめる
    assert client.put(f"/api/workouts/{w['id']}/menu", json={"menu_id": None}).json()["menu"] is None
