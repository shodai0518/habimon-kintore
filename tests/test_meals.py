from datetime import timedelta

import pytest

from app import config, food_ai
from conftest import NOW

RICE = {"name": "ご飯", "amount": "茶碗1杯・150g", "kcal": 234, "protein": 3.8, "fat": 0.5, "carbs": 55.7}
CHICKEN = {"name": "唐揚げ", "amount": "4個・120g", "kcal": 350, "protein": 20, "fat": 22, "carbs": 15}
PHOTO = "data:image/jpeg;base64," + "A" * 400


@pytest.fixture
def ai(monkeypatch):
    """AIの にせもの。calls に 渡されたものがたまる。"""
    calls = []

    def fake(images, note, answers=None):
        calls.append({"images": images, "note": note, "answers": answers})
        asked = bool(answers)
        return food_ai.FoodEstimate(
            kind="meal", items=[food_ai.FoodItem(**RICE), food_ai.FoodItem(**CHICKEN)],
            questions=[] if asked else ["ご飯は大盛りですか？"], comment="ご飯は並盛として計算")

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(food_ai, "estimate", fake)
    return calls


def test_analyze_then_save(client, ai):
    res = client.post("/api/meals/analyze", json={"images": [PHOTO], "note": "唐揚げ定食"}).json()
    assert [i["name"] for i in res["items"]] == ["ご飯", "唐揚げ"] and res["questions"] == ["ご飯は大盛りですか？"]
    assert res["ai"]["used"] == 1
    assert client.get("/api/meals").json()["meals"] == []  # 見積もっただけでは 記録されない
    # 質問に答えて 見積もり直す
    res = client.post("/api/meals/analyze", json={"images": [PHOTO], "note": "唐揚げ定食",
                                                   "questions": ["ご飯は大盛りですか？"], "answers": ["並です"]}).json()
    assert res["questions"] == [] and ai[-1]["answers"] == [("ご飯は大盛りですか？", "並です")]
    # 自分で直して 記録する（ご飯を 大盛りに）
    items = [{**RICE, "amount": "大盛り・250g", "kcal": 390, "protein": 6.3, "fat": 0.8, "carbs": 92.8}, CHICKEN]
    meal = client.post("/api/meals", json={"items": items, "note": "唐揚げ定食", "source": "photo"}).json()
    assert meal["kcal"] == 740 and meal["protein"] == 26.3 and meal["title"] == "ご飯、唐揚げ"
    data = client.get("/api/meals").json()
    assert data["totals"] == {"kcal": 740, "protein": 26.3, "fat": 22.8, "carbs": 107.8, "count": 1}
    assert data["meals"][0]["items"][0]["amount"] == "大盛り・250g"
    assert "PHOTO" not in str(data) and "base64" not in str(data)  # 写真は 保存しない


def test_targets_from_body_weight(client):
    assert client.get("/api/meals").json()["targets"] is None  # 体重の記録がないと 目標は出せない
    w = client.post("/api/workouts/start", json={"body_weight": 70}).json()
    client.delete(f"/api/workouts/{w['id']}")
    w = client.post("/api/workouts/start", json={"body_weight": 70}).json()
    assert client.get("/api/meals").json()["targets"] == {"weight": 70, "protein_per_kg": 2.0, "protein_min": 140, "fat_min": 42, "fat_max": 70}


def test_limits_and_validation(client, ai, monkeypatch):
    assert client.post("/api/meals/analyze", json={"images": [], "note": ""}).status_code == 400
    assert client.post("/api/meals/analyze", json={"images": ["http://example.com/a.jpg"]}).status_code == 400
    assert client.post("/api/meals/analyze", json={"images": [PHOTO] * 5}).status_code == 422  # 写真は4枚まで
    big = "data:image/jpeg;base64," + "A" * (config.FOOD_IMAGE_MAX_BYTES * 2)
    assert client.post("/api/meals/analyze", json={"images": [big]}).status_code == 400
    assert ai == []  # ここまで AIは 1回も呼ばれていない
    monkeypatch.setattr(config, "FOOD_AI_DAILY_LIMIT", 2)
    for _ in range(2):
        assert client.post("/api/meals/analyze", json={"note": "牛丼並盛"}).status_code == 200
    assert client.post("/api/meals/analyze", json={"note": "牛丼並盛"}).status_code == 429  # 1日の上限
    assert client.post("/api/meals", json={"items": []}).status_code == 422
    assert client.post("/api/meals", json={"items": [{**RICE, "kcal": -1}]}).status_code == 422


def test_without_ai_key_manual_entry_works(client):
    assert client.get("/api/meals").json()["ai"]["available"] is False
    assert client.post("/api/meals/analyze", json={"note": "牛丼"}).status_code == 503
    meal = client.post("/api/meals", json={"items": [RICE]}).json()  # 自分で数字を入れれば 記録できる
    assert meal["source"] == "manual" and meal["kcal"] == 234


def test_not_food_and_ai_failure(client, ai, monkeypatch):
    monkeypatch.setattr(food_ai, "estimate", lambda *a, **k: food_ai.FoodEstimate(kind="not_food", items=[], questions=[], comment=""))
    assert client.post("/api/meals/analyze", json={"images": [PHOTO]}).status_code == 400
    monkeypatch.setattr(food_ai, "estimate", lambda *a, **k: None)
    assert client.post("/api/meals/analyze", json={"images": [PHOTO]}).status_code == 502


def test_day_boundary_history_delete_and_sync(client):
    late = NOW.replace(hour=1, minute=30)  # 夜中の1:30 → 前の日の分
    old = client.post("/api/meals", json={"items": [CHICKEN], "eaten_at": (late - timedelta(days=1)).isoformat()}).json()
    assert old["day"] == (NOW - timedelta(days=2)).date().isoformat()
    today = client.post("/api/meals", json={"items": [RICE]}).json()
    data = client.get("/api/meals").json()
    assert [m["id"] for m in data["meals"]] == [today["id"]]
    assert data["history"] == [{"day": old["day"], "kcal": 350, "protein": 20.0, "fat": 22.0, "carbs": 15.0, "count": 1}]
    synced = client.get(f"/api/sync/meals?since={old['day']}").json()["meals"]
    assert [m["id"] for m in synced] == [old["id"], today["id"]] and synced[0]["items"][0]["name"] == "唐揚げ"
    assert client.delete(f"/api/meals/{old['id']}").json()["ok"]
    assert client.get("/api/meals").json()["history"] == []
    assert client.post("/api/meals", json={"items": [RICE], "eaten_at": (NOW + timedelta(hours=2)).isoformat()}).status_code == 400


def test_clean_item_limits_numbers():
    item = food_ai.clean_item(food_ai.FoodItem(name="  ", amount="x", kcal=-5, protein=9999, fat=1.26, carbs=3))
    assert (item.name, item.kcal, item.protein, item.fat) == ("（名前なし）", 0.0, 500.0, 1.3)


# ---- マイメニュー ----
OIKOS = {"name": "オイコス プレーン砂糖不使用", "amount": "1個・113g", "kcal": 92, "protein": 12.0, "fat": 0, "carbs": 5.4}


def test_menu_register_eat_and_edit(client):
    # 記録するときに マイメニューにも登録する
    client.post("/api/meals", json={"items": [OIKOS], "source": "label", "menu_name": "オイコス"})
    data = client.get("/api/meals").json()
    menu = data["menu"][0]
    assert (menu["name"], menu["kcal"], menu["protein"]) == ("オイコス", 92, 12.0) and data["servings"][1] == 1
    # 選ぶだけで記録できる（AIは使わない）。2個ぶんも
    meal = client.post(f"/api/menu/{menu['id']}/eat", json={}).json()
    assert (meal["title"], meal["kcal"], meal["source"]) == ("オイコス", 92, "menu")
    double = client.post(f"/api/menu/{menu['id']}/eat", json={"servings": 2}).json()
    assert (double["title"], double["kcal"], double["protein"]) == ("オイコス×2", 184, 24.0)
    data = client.get("/api/meals").json()
    assert data["totals"]["count"] == 3 and data["menu"][0]["use_count"] == 2 and data["ai"]["used"] == 0
    # 数字を直す
    fixed = client.put(f"/api/menu/{menu['id']}", json={"name": "オイコス", "items": [{**OIKOS, "kcal": 95}]}).json()
    assert fixed["kcal"] == 95
    assert client.delete(f"/api/menu/{menu['id']}").json()["ok"]
    assert client.get("/api/meals").json()["menu"] == []


def test_menu_from_meal_and_duplicates(client):
    meal = client.post("/api/meals", json={"items": [RICE, CHICKEN]}).json()
    menu = client.post(f"/api/menu/from-meal/{meal['id']}", json={"name": "唐揚げ定食"}).json()
    assert menu["kcal"] == 584 and [i["name"] for i in menu["items"]] == ["ご飯", "唐揚げ"]
    assert client.post(f"/api/menu/from-meal/{meal['id']}", json={"name": "唐揚げ定食"}).status_code == 409  # 同じ名前
    assert client.post("/api/menu", json={"name": "  ", "items": [RICE]}).status_code == 400
    assert client.post("/api/menu/999/eat", json={}).status_code == 404
    # 最近使ったものが 上に来る
    other = client.post("/api/menu", json={"name": "ご飯だけ", "items": [RICE]}).json()
    client.post(f"/api/menu/{menu['id']}/eat", json={})
    assert [m["name"] for m in client.get("/api/meals").json()["menu"]] == ["唐揚げ定食", "ご飯だけ"]
    assert other["use_count"] == 0


# ---- 基礎代謝 ----
def test_bmr_and_energy_balance(client):
    assert client.get("/api/meals").json()["energy"] == {"bmr": None, "workout_kcal": 0, "burned": None, "balance": None}
    # 体重と体脂肪率があれば Katch-McArdle：370 + 21.6 × (70 × 0.85)
    w = client.post("/api/workouts/start", json={"body_weight": 70, "body_fat": 15}).json()
    bmr = client.get("/api/workouts").json()["bmr"]
    assert bmr["value"] == round(370 + 21.6 * 70 * 0.85) and "Katch" in bmr["formula"] and bmr["body_fat"] == 15
    client.post("/api/meals", json={"items": [RICE, CHICKEN]})  # 584kcal
    energy = client.get("/api/meals").json()["energy"]
    assert energy["bmr"]["value"] == 1655 and energy["burned"] == 1655 + energy["workout_kcal"]
    assert energy["balance"] == 584 - energy["burned"]
    client.delete(f"/api/workouts/{w['id']}")


def test_bmr_from_profile_when_no_body_fat(client):
    client.post("/api/workouts/start", json={"body_weight": 70})
    assert client.get("/api/workouts").json()["bmr"] is None  # 体脂肪率も プロフィールも ない
    assert client.put("/api/sync/profile", json={"height": 175, "birth_date": "2000-10-01", "sex": "male"}).json()["ok"]
    bmr = client.get("/api/workouts").json()["bmr"]
    expected = (0.0481 * 70 + 0.0234 * 175 - 0.0138 * 26 - 0.4235) * 1000 / 4.186  # 2026-10-10 で 26歳
    assert bmr["value"] == round(expected) and "国立" in bmr["formula"]
    assert client.put("/api/sync/profile", json={"birth_date": "なし"}).status_code == 400
    assert client.put("/api/sync/profile", json={"sex": "x"}).status_code == 422


def test_four_photos_are_all_sent_to_ai(client, ai):
    """いくつかの商品の成分表示などを、4枚まで 1回で送れる。"""
    photos = ["data:image/jpeg;base64," + c * 400 for c in "ABCD"]
    assert client.post("/api/meals/analyze", json={"images": photos}).status_code == 200
    assert ai[-1]["images"] == photos and client.get("/api/meals").json()["max_images"] == 4


def test_menu_favorites_come_first(client):
    a = client.post("/api/menu", json={"name": "オイコス", "items": [OIKOS]}).json()
    b = client.post("/api/menu", json={"name": "唐揚げ定食", "items": [RICE, CHICKEN]}).json()
    assert a["favorite"] is False
    client.post(f"/api/menu/{b['id']}/eat", json={})  # 最近使ったのは 唐揚げ定食
    assert [m["name"] for m in client.get("/api/meals").json()["menu"]] == ["唐揚げ定食", "オイコス"]
    assert client.put(f"/api/menu/{a['id']}/favorite", json={"favorite": True}).json()["favorite"] is True
    menu = client.get("/api/meals").json()["menu"]
    assert [(m["name"], m["favorite"]) for m in menu] == [("オイコス", True), ("唐揚げ定食", False)]  # お気に入りが先
    client.put(f"/api/menu/{a['id']}/favorite", json={"favorite": False})
    assert [m["name"] for m in client.get("/api/meals").json()["menu"]] == ["唐揚げ定食", "オイコス"]
    assert client.put("/api/menu/999/favorite", json={"favorite": True}).status_code == 404


def test_todays_meals_are_newest_first(client, monkeypatch):
    from app import clock

    for hour, name in ((9, "朝ごはん"), (12, "昼ごはん"), (19, "夜ごはん")):
        monkeypatch.setattr(clock, "now", lambda hour=hour: NOW.replace(hour=hour))
        client.post("/api/meals", json={"items": [{**RICE, "name": name}]})
    assert [m["title"] for m in client.get("/api/meals").json()["meals"]] == ["夜ごはん", "昼ごはん", "朝ごはん"]
    # ハビットモンスターへ渡すときは 今までどおり 古い順
    assert [m["title"] for m in client.get("/api/sync/meals").json()["meals"]] == ["朝ごはん", "昼ごはん", "夜ごはん"]



def test_estimate_one_item(client, monkeypatch):
    """品目を1つだけ AIで計算する（名前と量は 本人が書いたもの）。回数は 写真の見積もりとは別に数える。"""
    calls = []

    def fake(name, amount, context=""):
        calls.append((name, amount, context))
        return food_ai.FoodItem(name=name, amount=amount or "1杯・200g", kcal=350, protein=20, fat=10, carbs=40)

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(food_ai, "estimate_item", fake)
    res = client.post("/api/meals/estimate-item", json={"name": "豚汁", "amount": "", "context": "唐揚げ定食、ご飯"}).json()
    assert res == {"name": "豚汁", "amount": "1杯・200g", "kcal": 350, "protein": 20, "fat": 10, "carbs": 40}
    assert calls == [("豚汁", "", "唐揚げ定食、ご飯")]
    assert client.get("/api/meals").json()["ai"]["used"] == 0  # 写真の見積もりの回数は 使わない

    from app import config
    monkeypatch.setattr(config, "FOOD_ITEM_AI_DAILY_LIMIT", 1)
    assert client.post("/api/meals/estimate-item", json={"name": "味噌汁"}).status_code == 429
    assert client.post("/api/meals/estimate-item", json={"name": ""}).status_code == 422


def test_estimate_item_without_ai(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert client.post("/api/meals/estimate-item", json={"name": "豚汁"}).status_code == 503
