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
    assert client.get("/api/meals").json()["targets"] == {"weight": 70, "protein_min": 112, "fat_min": 42, "fat_max": 70}


def test_limits_and_validation(client, ai, monkeypatch):
    assert client.post("/api/meals/analyze", json={"images": [], "note": ""}).status_code == 400
    assert client.post("/api/meals/analyze", json={"images": ["http://example.com/a.jpg"]}).status_code == 400
    assert client.post("/api/meals/analyze", json={"images": [PHOTO] * 3}).status_code == 422  # 写真は2枚まで
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
