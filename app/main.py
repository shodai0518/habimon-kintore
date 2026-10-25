"""ハビモン筋トレ：筋トレの記録だけをする 小さなアプリ。

スマホで記録 → ハビットモンスターが /api/sync から取りこむ → ハビモンで 経験値などがもらえる。
起動（パソコンで試すとき）: KINTORE_DEV=1 python -m uvicorn app.main:app --port 8100
"""

import base64
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import auth, calorie, clock, config, food_ai, menu_ai, push, rules
from app.db import engine, get_session, init_db
from app.models import AiUsage, AppSetting, Exercise, Meal, MenuItem, Workout, WorkoutMenu, WorkoutSet

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PartKey = Literal["chest", "shoulders", "arms", "back", "abs", "legs", "cardio"]


# ---- 入力 ----
class LoginInput(BaseModel):
    password: str = Field(max_length=200)


class StartInput(BaseModel):
    menu_id: int | None = None  # 使う 筋トレメニュー
    body_weight: float | None = Field(default=None, gt=0, lt=500)
    body_fat: float | None = Field(default=None, gt=0, lt=80)
    started_at: datetime | None = None  # あとから記録するときに 指定する
    planned_end: datetime | None = None


class UpdateInput(BaseModel):
    body_weight: float | None = Field(default=None, gt=0, lt=500)
    body_fat: float | None = Field(default=None, gt=0, lt=80)
    memo: str | None = Field(default=None, max_length=200)


class SetInput(BaseModel):
    exercise_id: int
    weight: float | None = Field(default=None, ge=0, lt=1000)
    reps: int | None = Field(default=None, ge=1, le=1000)
    duration_min: float | None = Field(default=None, gt=0, le=600)
    distance_km: float | None = Field(default=None, gt=0, le=500)


class WarmupInput(BaseModel):
    exercise_id: int
    sets: int = Field(ge=0, le=config.WARMUP_MAX_SETS)


class PlannedEndInput(BaseModel):
    planned_end: datetime


class FinishInput(BaseModel):
    ended_at: datetime | None = None


class RestInput(BaseModel):
    seconds: int = Field(ge=config.REST_MIN_SECONDS, le=config.REST_MAX_SECONDS)


class ExerciseInput(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    part: PartKey


def naive(moment: datetime | None) -> datetime | None:
    return moment.replace(tzinfo=None) if moment else None


# ---- 種目 ----
def ensure_presets(session: Session) -> None:
    existing = {e.name for e in session.exec(select(Exercise)).all()}
    for part, names in config.PRESET_EXERCISES.items():
        for name in names:
            if name not in existing:
                session.add(Exercise(name=name, part=part, preset=True, mets=config.EXERCISE_METS.get(name)))
    session.commit()


def done_sets(session: Session, exercise_id: int, before: datetime | None = None) -> list[tuple[Workout, list[WorkoutSet]]]:
    """その種目を やった記録（新しい順）。"""
    query = (
        select(Workout, WorkoutSet)
        .join(WorkoutSet, WorkoutSet.workout_id == Workout.id)
        .where(WorkoutSet.exercise_id == exercise_id, Workout.status == "done")
        .order_by(Workout.started_at.desc(), WorkoutSet.id)
    )
    if before is not None:
        query = query.where(Workout.started_at < before)
    grouped: dict[int, tuple[Workout, list[WorkoutSet]]] = {}
    for w, s in session.exec(query).all():
        grouped.setdefault(w.id, (w, []))[1].append(s)
    return list(grouped.values())


def rest_seconds(ex: Exercise) -> int | None:
    """セットの間の休憩（秒）。自分で変えていれば それ、なければ 決まった長さ。有酸素は なし。"""
    if ex.part == "cardio":
        return None
    if ex.rest_seconds:
        return ex.rest_seconds
    if ex.name in config.BIG3_EXERCISES:
        return config.REST_BIG3_SECONDS
    return config.REST_SECONDS_BY_PART[ex.part]


def exercise_view(session: Session, ex: Exercise) -> dict:
    history = done_sets(session, ex.id)
    last = history[0] if history else None
    best = rules.best_set([s for _, sets in history for s in sets]) if ex.part != "cardio" else None
    return {
        "id": ex.id, "name": ex.name, "part": ex.part, "part_label": config.BODY_PARTS[ex.part],
        "cardio": ex.part == "cardio", "warmup_sets": ex.warmup_sets, "rest_seconds": rest_seconds(ex),
        "last": {"day": last[0].day, "sets": [rules.set_label(s) for s in last[1]],
                 "weight": last[1][-1].weight, "reps": last[1][-1].reps,
                 "duration_min": last[1][-1].duration_min, "distance_km": last[1][-1].distance_km} if last else None,
        "best": rules.set_label(best) if best else None,
    }


# ---- 筋トレ ----
def rows_of(session: Session, workout_id: int) -> list[tuple[WorkoutSet, Exercise]]:
    return session.exec(
        select(WorkoutSet, Exercise).join(Exercise, Exercise.id == WorkoutSet.exercise_id)
        .where(WorkoutSet.workout_id == workout_id).order_by(WorkoutSet.id)
    ).all()


def weight_for(session: Session, w: Workout) -> float | None:
    """カロリーの計算に使う体重：その筋トレで入れた値、なければ それより前の 最後の値。"""
    if w.body_weight:
        return w.body_weight
    previous = session.exec(
        select(Workout.body_weight).where(Workout.body_weight.is_not(None), Workout.started_at <= w.started_at)
        .order_by(Workout.started_at.desc())
    ).first()
    return previous


def calories_for(session: Session, w: Workout, rows: list[tuple[WorkoutSet, Exercise]]) -> dict:
    if w.minutes is not None:
        minutes, end = w.minutes, w.ended_at
    else:  # 記録中：あとから記録なら 入れた終了時刻まで、今やっているなら 今まで
        end = w.planned_end or clock.now()
        minutes = min(config.WORKOUT_MAX_MINUTES, max(0, (end - w.started_at).total_seconds() / 60))
    sets = [{"name": ex.name, "mets": ex.mets, "cardio": ex.part == "cardio", "at": s.created_at,
             "duration_min": s.duration_min, "distance_km": s.distance_km} for s, ex in rows]
    warmups = json.loads(w.warmups) if w.warmups else {}
    names = {str(ex.id): ex.name for _, ex in rows}
    weight = weight_for(session, w)
    result = calorie.workout_calories(minutes, sets, weight, {names[k]: n for k, n in warmups.items() if k in names},
                                      started_at=w.started_at, ended_at=end)  # 実際の休憩時間で数える
    return {**result, "weight_used": weight}


def workout_view(session: Session, w: Workout) -> dict:
    rows = rows_of(session, w.id)
    groups: dict[int, dict] = {}
    for s, ex in rows:
        g = groups.setdefault(ex.id, {"exercise_id": ex.id, "name": ex.name, "part": ex.part,
                                      "part_label": config.BODY_PARTS[ex.part], "sets": []})
        g["sets"].append({"id": s.id, "label": rules.set_label(s), "is_pr": s.is_pr, "weight": s.weight, "reps": s.reps,
                          "duration_min": s.duration_min, "distance_km": s.distance_km})
    calories = calories_for(session, w, rows)
    warmups = json.loads(w.warmups) if w.warmups else {}
    for g in groups.values():
        g["kcal"] = calories["by_exercise"].get(g["name"])
        g["warmup"] = warmups.get(str(g["exercise_id"]), 0)
    return {
        "id": w.id, "day": w.day, "started_at": w.started_at, "ended_at": w.ended_at, "planned_end": w.planned_end,
        "status": w.status, "body_weight": w.body_weight, "body_fat": w.body_fat, "memo": w.memo, "minutes": w.minutes,
        "warmups": warmups, "exercises": list(groups.values()), "set_count": len(rows),
        "kcal": calories["total"], "kcal_weight": calories["weight_used"],
        "menu": workout_menu_progress(session, w, rows),
    }


def workout_menu_progress(session: Session, w: Workout, rows) -> dict | None:
    """この筋トレの メニューと、種目ごとに 何セット やったか（えらんだ代わりの種目も 数える）。"""
    menu = session.get(WorkoutMenu, w.menu_id) if w.menu_id else None
    if menu is None:
        return None
    done: dict[int, int] = {}
    for _, ex in rows:
        done[ex.id] = done.get(ex.id, 0) + 1
    view = workout_menu_view(session, menu)
    for item in view["items"]:
        ids = [item["exercise_id"]] + [a["id"] for a in item["alternatives"]]
        item["done"] = sum(done.get(i, 0) for i in ids)
        used = next((i for i in ids if done.get(i)), None)
        item["used_id"] = used  # 実際に やった種目（代わりの種目のときも）
    return view


def get_active(session: Session) -> Workout | None:
    return session.exec(select(Workout).where(Workout.status == "active")).first()


def load(session: Session, workout_id: int, status: str | None = None) -> Workout:
    w = session.get(Workout, workout_id)
    if w is None or (status and w.status != status):
        raise HTTPException(404, "その筋トレの記録は見つかりません")
    return w


def mark_prs(session: Session, w: Workout, rows: list[tuple[WorkoutSet, Exercise]]) -> list[str]:
    """この筋トレで 自己ベストを更新した種目に 印をつける（前の記録がない種目は のぞく）。"""
    by_exercise: dict[int, tuple[Exercise, list[WorkoutSet]]] = {}
    for s, ex in rows:
        if ex.part != "cardio":
            by_exercise.setdefault(ex.id, (ex, []))[1].append(s)
    messages = []
    for ex, sets in by_exercise.values():
        before = [s for _, old in done_sets(session, ex.id, before=w.started_at) for s in old]
        old_best, new_best = rules.best_set(before), rules.best_set(sets)
        if old_best and new_best and rules.one_rep_max(new_best.weight, new_best.reps) > rules.one_rep_max(old_best.weight, old_best.reps):
            new_best.is_pr = True
            messages.append(f"{ex.name} 自己ベスト更新！ {rules.set_label(new_best)}")
    return messages


# ---- 画面から使う API（ログインが必要）----
api = APIRouter(prefix="/api", dependencies=[Depends(auth.require_login)])


@api.get("/exercises")
def list_exercises(session: Session = Depends(get_session)):
    exercises = session.exec(select(Exercise).order_by(Exercise.id)).all()
    return {"parts": config.BODY_PARTS, "exercises": [exercise_view(session, e) for e in exercises],
            "rest": {"step": config.REST_STEP_SECONDS, "min": config.REST_MIN_SECONDS, "max": config.REST_MAX_SECONDS}}


@api.put("/exercises/{exercise_id}/rest")
def set_rest(exercise_id: int, body: RestInput, session: Session = Depends(get_session)):
    """この種目の 休憩の長さを覚える。"""
    ex = session.get(Exercise, exercise_id)
    if ex is None or ex.part == "cardio":
        raise HTTPException(404, "その種目は見つかりません")
    ex.rest_seconds = body.seconds
    session.commit()
    return {"rest_seconds": rest_seconds(ex)}


# ---- 筋トレメニュー ----
class MenuItemInput(BaseModel):
    exercise: str = Field(min_length=1, max_length=30)  # 種目の名前（なければ 作る）
    part: PartKey = "legs"  # 新しく作るときの 部位
    alternatives: list[str] = Field(default=[], max_length=3)
    sets_min: int = Field(default=3, ge=1, le=20)
    sets_max: int = Field(default=3, ge=1, le=20)
    reps: str = Field(default="", max_length=30)
    note: str = Field(default="", max_length=80)


class WorkoutMenuInput(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    items: list[MenuItemInput] = Field(min_length=1, max_length=config.MENU_ITEMS_MAX)


class MenuTextInput(BaseModel):
    text: str = Field(min_length=10, max_length=6000)


class WorkoutMenuChoice(BaseModel):
    menu_id: int | None = None


def find_or_create_exercise(session: Session, name: str, part: str) -> Exercise:
    name = name.strip()[:30]
    ex = session.exec(select(Exercise).where(Exercise.name == name)).first()
    if ex is None:
        ex = Exercise(name=name, part=part, mets_estimated=False,
                      mets=config.DEFAULT_CARDIO_METS if part == "cardio" else config.DEFAULT_METS)
        session.add(ex)
        session.flush()
    return ex


def workout_menu_view(session: Session, menu: WorkoutMenu) -> dict:
    items = []
    for raw in json.loads(menu.items):
        ex = session.get(Exercise, raw["exercise_id"])
        if ex is None:
            continue
        alts = [a for a in (session.get(Exercise, i) for i in raw.get("alternative_ids", [])) if a]
        items.append({
            "exercise_id": ex.id, "name": ex.name, "part": ex.part, "part_label": config.BODY_PARTS[ex.part],
            "alternatives": [{"id": a.id, "name": a.name, "part": a.part} for a in alts],
            "sets_min": raw["sets_min"], "sets_max": raw["sets_max"], "reps": raw.get("reps", ""), "note": raw.get("note", ""),
        })
    return {"id": menu.id, "name": menu.name, "items": items}


def save_workout_menu(session: Session, body: WorkoutMenuInput, menu: WorkoutMenu | None = None) -> WorkoutMenu:
    items = []
    for item in body.items:
        ex = find_or_create_exercise(session, item.exercise, item.part)
        alts = [find_or_create_exercise(session, name, item.part).id for name in item.alternatives if name.strip()]
        items.append({"exercise_id": ex.id, "alternative_ids": alts, "sets_min": min(item.sets_min, item.sets_max),
                      "sets_max": max(item.sets_min, item.sets_max), "reps": item.reps.strip(), "note": item.note.strip()})
    if menu is None:
        if len(session.exec(select(WorkoutMenu)).all()) >= config.MENU_MAX:
            raise HTTPException(409, f"メニューは{config.MENU_MAX}個までです")
        menu = WorkoutMenu(name=body.name.strip(), position=len(session.exec(select(WorkoutMenu)).all()), created_at=clock.now())
    menu.name = body.name.strip()
    menu.items = json.dumps(items, ensure_ascii=False)
    session.add(menu)
    session.commit()
    session.refresh(menu)
    return menu


@api.get("/workout-menus")
def read_workout_menus(session: Session = Depends(get_session)):
    menus = session.exec(select(WorkoutMenu).order_by(WorkoutMenu.position, WorkoutMenu.id)).all()
    usage = session.get(AiUsage, clock.today())
    return {"menus": [workout_menu_view(session, m) for m in menus], "ai": {"available": menu_ai.available(),
            "left": config.MENU_AI_DAILY_LIMIT - (usage.menu_count if usage else 0)},
            "parts": config.BODY_PARTS, "exercises": [e.name for e in session.exec(select(Exercise).order_by(Exercise.name))]}


@api.post("/workout-menus/parse")
def parse_workout_menus(body: MenuTextInput, session: Session = Depends(get_session)):
    """メニューの文を AIで読み取る（まだ保存しない。画面で確かめて 直してから 保存する）。"""
    if not menu_ai.available():
        raise HTTPException(503, "AIがまだ設定されていません。自分で入れてね")
    usage = session.get(AiUsage, clock.today()) or AiUsage(day=clock.today(), count=0)
    if usage.menu_count >= config.MENU_AI_DAILY_LIMIT:
        raise HTTPException(429, f"今日は もう{config.MENU_AI_DAILY_LIMIT}回 読み取りました")
    usage.menu_count += 1
    session.add(usage)
    session.commit()
    existing = [(e.name, e.part) for e in session.exec(select(Exercise))]
    plan = menu_ai.parse(body.text, existing)
    if plan is None or not plan.menus:
        raise HTTPException(502, "読み取れなかった。少し待ってから もう一度試してね")
    names = {name for name, _ in existing}
    return {"menus": [{"name": m.name[:30], "items": [{**i.model_dump(), "exists": i.exercise in names} for i in m.items]}
                      for m in plan.menus]}


@api.post("/workout-menus")
def create_workout_menu(body: WorkoutMenuInput, session: Session = Depends(get_session)):
    return workout_menu_view(session, save_workout_menu(session, body))


@api.put("/workout-menus/{menu_id}")
def update_workout_menu(menu_id: int, body: WorkoutMenuInput, session: Session = Depends(get_session)):
    menu = session.get(WorkoutMenu, menu_id)
    if menu is None:
        raise HTTPException(404, "そのメニューは見つかりません")
    return workout_menu_view(session, save_workout_menu(session, body, menu))


@api.delete("/workout-menus/{menu_id}")
def delete_workout_menu(menu_id: int, session: Session = Depends(get_session)):
    menu = session.get(WorkoutMenu, menu_id)
    if menu is None:
        raise HTTPException(404, "そのメニューは見つかりません")
    for w in session.exec(select(Workout).where(Workout.menu_id == menu_id)):
        w.menu_id = None
    session.delete(menu)
    session.commit()
    return {"ok": True}


@api.put("/workouts/{workout_id}/menu")
def choose_workout_menu(workout_id: int, body: WorkoutMenuChoice, session: Session = Depends(get_session)):
    """筋トレの途中で メニューを えらぶ・やめる。"""
    w = load(session, workout_id, "active")
    if body.menu_id is not None and session.get(WorkoutMenu, body.menu_id) is None:
        raise HTTPException(404, "そのメニューは見つかりません")
    w.menu_id = body.menu_id
    session.commit()
    return workout_view(session, w)


@api.post("/exercises")
def create_exercise(body: ExerciseInput, session: Session = Depends(get_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "種目の名前を入れてね")
    if session.exec(select(Exercise).where(Exercise.name == name)).first():
        raise HTTPException(409, "その種目は もうあります")
    # 強さ（METs）は 仮の値。ハビモンが取りこむときに AIで決め直して、ここにも書きもどす
    ex = Exercise(name=name, part=body.part, mets_estimated=False,
                  mets=config.DEFAULT_CARDIO_METS if body.part == "cardio" else config.DEFAULT_METS)
    session.add(ex)
    session.commit()
    session.refresh(ex)
    return exercise_view(session, ex)


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=10, max_length=200)
    auth: str = Field(min_length=10, max_length=100)


class PushSubscriptionInput(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1000)
    keys: PushKeys


class PushEndpointInput(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1000)


class RestNotifyInput(BaseModel):
    seconds: float = Field(ge=0, le=3600)  # 休憩が終わるまでの秒数（送った時点）
    end_at: float | None = None  # 休憩が終わる時刻（1970年からの ミリ秒。スマホの時計）
    name: str = Field(default="", max_length=60)  # 種目の名前


CLOCK_SKEW_LIMIT = 120  # スマホとサーバーの時計が これ以上ずれていたら、時刻ではなく 秒数を使う


def rest_delay(body: RestNotifyInput, now: float | None = None) -> float:
    """あと何秒で 通知を送るか。終わる時刻で決める（サーバーが起きるのを待った分や 通信の遅れで ずれないように）。
    スマホの時計が 大きくずれているときだけ、送られてきた秒数を使う。"""
    if body.end_at is None:
        return body.seconds
    left = body.end_at / 1000 - (time.time() if now is None else now)
    if abs(left - body.seconds) > CLOCK_SKEW_LIMIT:
        return body.seconds
    return max(0.0, left)


@api.get("/push")
def read_push(session: Session = Depends(get_session)):
    """ロック画面への通知が使えるか（サーバーに鍵が入っているか）と、登録に使う公開鍵。"""
    return {"available": push.configured(), "public_key": push.public_key() if push.configured() else None,
            "count": len(push.subscriptions(session))}


@api.post("/push/subscribe")
def push_subscribe(body: PushSubscriptionInput, session: Session = Depends(get_session)):
    """このスマホを 通知の送り先として覚える。"""
    if not push.configured():
        raise HTTPException(503, "通知の設定が まだできていません")
    if not push.allowed_endpoint(body.endpoint):
        raise HTTPException(400, "この送り先には 通知を送れません")
    push.add(session, body.model_dump())
    return {"ok": True}


@api.post("/push/unsubscribe")
def push_unsubscribe(body: PushEndpointInput, session: Session = Depends(get_session)):
    push.remove(session, body.endpoint)
    return {"ok": True}


@api.post("/push/test")
def push_test(session: Session = Depends(get_session)):
    """5秒後に ためしの通知を送る（その間に 画面をロックして たしかめる）。"""
    if not push.configured() or not push.subscriptions(session):
        raise HTTPException(400, "通知が まだオンになっていません")
    push.schedule(5 - push.GRACE_SECONDS, {**push.partner_message(get_partner(session), "test"), "tag": "test"})
    return {"ok": True, "seconds": 5}


@api.post("/rest/notify")
def rest_notify(body: RestNotifyInput, session: Session = Depends(get_session)):
    """休憩が終わる時刻に 通知を送るように 予約する（前の予約は 置きかえる）。相棒が話しかける形にする。"""
    if not push.configured() or not push.subscriptions(session):
        return {"scheduled": False}
    push.schedule(rest_delay(body), {**push.partner_message(get_partner(session), "rest", body.name), "tag": "rest"})
    return {"scheduled": True}


@api.delete("/rest/notify")
def rest_notify_cancel():
    """休憩をやめた・アプリが自分で知らせた → 予約を取り消す。"""
    return {"cancelled": push.cancel()}


def get_partner(session: Session) -> dict | None:
    setting = session.get(AppSetting, PARTNER_KEY)
    return json.loads(setting.value) if setting else None


@api.get("/partner")
def read_partner(session: Session = Depends(get_session)):
    """ハビットモンスターの相棒（まだ届いていなければ null）。"""
    partner = get_partner(session)
    if partner is None:
        return {"partner": None}
    return {"partner": {
        "name": partner["name"], "rank": partner["rank"], "level": partner["level"],
        "motion": partner.get("motion", "hop"),
        "comments": [{k: c[k] for k in ("kind", "label", "text")} for c in partner.get("comments") or []],
        # 画像が変わると 番号も変わるので、ずっと覚えておいてもらって大丈夫
        "image_url": f"/api/partner/image?v={partner['version']}" if partner.get("image") else None,
    }}


@api.get("/partner/image")
def read_partner_image(session: Session = Depends(get_session)):
    partner = get_partner(session)
    if partner is None or not partner.get("image"):
        raise HTTPException(404, "相棒の画像がありません")
    return Response(base64.b64decode(partner["image"]), media_type="image/webp",
                    headers={"Cache-Control": "private, max-age=31536000, immutable"})


@api.get("/workouts")
def read_workouts(session: Session = Depends(get_session)):
    since = clock.today() - timedelta(days=config.HISTORY_DAYS - 1)
    active = get_active(session)
    done = session.exec(
        select(Workout).where(Workout.status == "done", Workout.day >= since).order_by(Workout.started_at.desc())
    ).all()
    last_weight = session.exec(
        select(Workout).where(Workout.body_weight.is_not(None), Workout.status != "canceled").order_by(Workout.started_at.desc())
    ).first()
    canceled = session.exec(select(Workout).where(
        Workout.status == "canceled", Workout.day >= clock.today() - timedelta(days=config.CANCELED_KEEP_DAYS - 1))
        .order_by(Workout.started_at.desc())).all()
    return {
        "current": workout_view(session, active) if active else None,
        "canceled": [{"id": w.id, "day": w.day, "started_at": w.started_at, "set_count": len(rows_of(session, w.id))} for w in canceled],
        "history": [workout_view(session, w) for w in done],
        "last_weight": last_weight.body_weight if last_weight else None,
        "last_fat": last_weight.body_fat if last_weight else None,
        "backfill_days": config.WORKOUT_BACKFILL_DAYS,
        "now": clock.now(),
        "bmr": bmr_now(session),
    }


@api.post("/workouts/start")
def start(body: StartInput, session: Session = Depends(get_session)):
    if get_active(session):
        raise HTTPException(409, "記録中の筋トレがあります")
    now = clock.now()
    started = naive(body.started_at) or now
    if started > now + timedelta(minutes=5):
        raise HTTPException(400, "これからの時刻は選べません")
    if clock.game_day(started) < clock.today() - timedelta(days=config.WORKOUT_BACKFILL_DAYS):
        raise HTTPException(400, f"記録できるのは {config.WORKOUT_BACKFILL_DAYS}日前までです")
    planned_end = naive(body.planned_end)
    if body.menu_id is not None and session.get(WorkoutMenu, body.menu_id) is None:
        raise HTTPException(404, "そのメニューは見つかりません")
    w = Workout(day=clock.game_day(started), started_at=started,
                planned_end=planned_end if planned_end and planned_end > started else None,
                body_weight=body.body_weight, body_fat=body.body_fat, menu_id=body.menu_id)
    session.add(w)
    session.commit()
    session.refresh(w)
    return workout_view(session, w)


@api.patch("/workouts/{workout_id}")
def update(workout_id: int, body: UpdateInput, session: Session = Depends(get_session)):
    w = load(session, workout_id, "active")
    w.body_weight, w.body_fat, w.memo = body.body_weight, body.body_fat, body.memo
    session.commit()
    session.refresh(w)
    return workout_view(session, w)


@api.put("/workouts/{workout_id}/planned-end")
def set_planned_end(workout_id: int, body: PlannedEndInput, session: Session = Depends(get_session)):
    w = load(session, workout_id, "active")
    planned_end = naive(body.planned_end)
    if planned_end <= w.started_at:
        raise HTTPException(400, "終了時刻は、開始時刻よりあとにしてね")
    w.planned_end = planned_end
    session.commit()
    session.refresh(w)
    return workout_view(session, w)


@api.post("/workouts/{workout_id}/sets")
def add_set(workout_id: int, body: SetInput, session: Session = Depends(get_session)):
    w = load(session, workout_id, "active")
    ex = session.get(Exercise, body.exercise_id)
    if ex is None:
        raise HTTPException(404, "その種目は見つかりません")
    if ex.part == "cardio":
        if not body.duration_min and not body.distance_km:
            raise HTTPException(400, "時間か距離を入れてね")
        s = WorkoutSet(workout_id=w.id, exercise_id=ex.id, duration_min=body.duration_min, distance_km=body.distance_km)
    else:
        if not body.reps:
            raise HTTPException(400, "回数を入れてね")
        s = WorkoutSet(workout_id=w.id, exercise_id=ex.id, weight=body.weight or None, reps=body.reps)
    s.created_at = clock.now()
    session.add(s)
    warmups = json.loads(w.warmups) if w.warmups else {}
    if ex.part != "cardio" and str(ex.id) not in warmups:  # 選んでいなければ いつもの数
        warmups[str(ex.id)] = ex.warmup_sets
        w.warmups = json.dumps(warmups)
    session.commit()
    return workout_view(session, w)


@api.put("/workouts/{workout_id}/warmup")
def set_warmup(workout_id: int, body: WarmupInput, session: Session = Depends(get_session)):
    w = load(session, workout_id, "active")
    ex = session.get(Exercise, body.exercise_id)
    if ex is None or ex.part == "cardio":
        raise HTTPException(404, "その種目は見つかりません")
    warmups = json.loads(w.warmups) if w.warmups else {}
    warmups[str(ex.id)] = body.sets
    w.warmups = json.dumps(warmups)
    ex.warmup_sets = body.sets
    session.commit()
    session.refresh(w)
    return workout_view(session, w)


@api.delete("/workouts/sets/{set_id}")
def delete_set(set_id: int, session: Session = Depends(get_session)):
    s = session.get(WorkoutSet, set_id)
    if s is None or session.get(Workout, s.workout_id).status != "active":
        raise HTTPException(404, "そのセットは消せません")
    workout_id = s.workout_id
    session.delete(s)
    session.commit()
    return workout_view(session, session.get(Workout, workout_id))


@api.delete("/workouts/{workout_id}")
def cancel(workout_id: int, session: Session = Depends(get_session)):
    """記録中の筋トレを やめる。セットがあれば 消さずに しまっておき、数日のあいだは 元に戻せる
    （押しまちがえで 記録が消えないように）。セットがなければ そのまま消す。"""
    w = load(session, workout_id, "active")
    if not rows_of(session, w.id):
        session.delete(w)
        session.commit()
        return {"ok": True, "kept": False}
    w.status = "canceled"
    w.ended_at = clock.now()
    session.commit()
    return {"ok": True, "kept": True}


@api.post("/workouts/{workout_id}/restore")
def restore(workout_id: int, session: Session = Depends(get_session)):
    """やめた筋トレを 記録中に戻す。"""
    w = load(session, workout_id, "canceled")
    if get_active(session):
        raise HTTPException(409, "記録中の筋トレがあるので 戻せない。先に終わるか やめてね")
    w.status = "active"
    w.ended_at = None
    session.commit()
    return workout_view(session, w)


@api.post("/workouts/{workout_id}/finish")
def finish(workout_id: int, body: FinishInput, session: Session = Depends(get_session)):
    w = load(session, workout_id, "active")
    ended = naive(body.ended_at) or clock.now()
    minutes = int((ended - w.started_at).total_seconds() // 60)
    if minutes < 1:
        raise HTTPException(400, "終了時刻は 開始時刻より あとにしてね")
    if minutes > config.WORKOUT_MAX_MINUTES:
        raise HTTPException(400, f"1回の筋トレは {config.WORKOUT_MAX_MINUTES // 60}時間までです。時刻を確かめてね")
    rows = rows_of(session, w.id)
    if not rows:
        raise HTTPException(400, "セットを1つ以上 記録してね（やめるときは「やめる」）")
    messages = [f"筋トレ終了！ {minutes}分・{len(rows)}セット"] + mark_prs(session, w, rows)
    w.status, w.ended_at, w.minutes = "done", ended, minutes
    session.commit()
    session.refresh(w)
    view = workout_view(session, w)
    if view["kcal"]:
        messages.append(f"消費カロリー 約{view['kcal']}kcal")
    messages.append("ハビモンを開くと、経験値などのご褒美がもらえるよ！")
    return {"messages": messages, "workout": view}


# ---- 食事 ----
class AnalyzeInput(BaseModel):
    images: list[str] = Field(default=[], max_length=config.FOOD_MAX_IMAGES)  # data URL（画面側で 小さくした JPEG）
    note: str = Field(default="", max_length=300)
    questions: list[str] = Field(default=[], max_length=config.FOOD_MAX_QUESTIONS)  # 前に AIが聞いた質問
    answers: list[str] = Field(default=[], max_length=config.FOOD_MAX_QUESTIONS)  # その答え


class MealInput(BaseModel):
    items: list[food_ai.ItemInput] = Field(min_length=1, max_length=config.FOOD_MAX_ITEMS)
    note: str = Field(default="", max_length=300)
    source: Literal["photo", "text", "label", "manual"] = "manual"
    eaten_at: datetime | None = None  # あとから記録するとき
    menu_name: str | None = Field(default=None, max_length=40)  # 入れると、マイメニューにも登録する


class MenuInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    items: list[food_ai.ItemInput] = Field(min_length=1, max_length=config.FOOD_MAX_ITEMS)


class MenuFromMealInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class FavoriteInput(BaseModel):
    favorite: bool


class EatInput(BaseModel):
    servings: float = Field(default=1, gt=0, le=10)  # 何個（何人前）ぶん食べたか


def latest_weight(session: Session) -> float | None:
    return session.exec(
        select(Workout.body_weight).where(Workout.body_weight.is_not(None)).order_by(Workout.started_at.desc())
    ).first()


PROFILE_KEY = "profile"  # 身長・生年月日・性別（ハビットモンスターが 取りこみのついでに送ってくる）


def get_profile(session: Session) -> dict | None:
    setting = session.get(AppSetting, PROFILE_KEY)
    return json.loads(setting.value) if setting else None


def latest_body_fat(session: Session) -> float | None:
    return session.exec(
        select(Workout.body_fat).where(Workout.body_fat.is_not(None)).order_by(Workout.started_at.desc())
    ).first()


def bmr_now(session: Session) -> dict | None:
    """今の基礎代謝（目安）。体重と、体脂肪率 か プロフィール（身長・生年月日・性別）から。"""
    profile = get_profile(session) or {}
    weight, body_fat = latest_weight(session), latest_body_fat(session)
    result = calorie.bmr(
        weight, body_fat, profile.get("height"),
        datetime.fromisoformat(profile["birth_date"]).date() if profile.get("birth_date") else None,
        profile.get("sex"), clock.today(),
    )
    return {**result, "weight": weight, "body_fat": body_fat} if result else None


def workout_kcal_on(session: Session, day) -> int:
    """その日に 筋トレで使ったカロリー（終わったものと 記録中のもの）。"""
    total = 0
    for w in session.exec(select(Workout).where(Workout.day == day, Workout.status != "canceled")).all():
        total += calories_for(session, w, rows_of(session, w.id))["total"] or 0
    return total


def energy_today(session: Session, eaten_kcal: int | None = None) -> dict:
    """今日 使ったカロリー（基礎代謝 ＋ 筋トレ）と、食べたカロリーとの差。"""
    bmr = bmr_now(session)
    workout = workout_kcal_on(session, clock.today())
    burned = bmr["value"] + workout if bmr else None
    return {"bmr": bmr, "workout_kcal": workout, "burned": burned,
            "balance": eaten_kcal - burned if burned is not None and eaten_kcal is not None else None}


def nutrition_targets(weight: float | None) -> dict | None:
    """体重から決める 1日の目標。"""
    if not weight:
        return None
    return {"weight": weight, "protein_per_kg": config.PROTEIN_PER_KG, "protein_min": round(weight * config.PROTEIN_PER_KG),
            "fat_min": round(weight * config.FAT_PER_KG_MIN), "fat_max": round(weight * config.FAT_PER_KG_MAX)}


def health_targets(session: Session) -> dict:
    """食塩と食物繊維の目安（プロフィールの性別で決める。分からなければ 男性の値）。"""
    sex = "female" if (get_profile(session) or {}).get("sex") == "female" else "male"
    return {"salt_max": config.SALT_MAX[sex], "fiber_min": config.FIBER_MIN[sex]}


def round_opt(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def sum_opt(rows, key: str) -> float | None:
    """合計（どれにも数字がなければ None。食塩と食物繊維は、項目を作る前の記録に 数字がない）。"""
    values = [v for v in ((r.get(key) if isinstance(r, dict) else getattr(r, key)) for r in rows) if v is not None]
    return round(sum(values), 1) if values else None


def meal_view(meal: Meal) -> dict:
    return {"id": meal.id, "day": meal.day, "eaten_at": meal.eaten_at, "title": meal.title, "kcal": round(meal.kcal),
            "protein": round(meal.protein, 1), "fat": round(meal.fat, 1), "carbs": round(meal.carbs, 1),
            "salt": round_opt(meal.salt), "fiber": round_opt(meal.fiber),
            "items": json.loads(meal.items), "note": meal.note, "source": meal.source}


def day_totals(meals: list[Meal]) -> dict:
    return {"kcal": round(sum(m.kcal for m in meals)), "protein": round(sum(m.protein for m in meals), 1),
            "fat": round(sum(m.fat for m in meals), 1), "carbs": round(sum(m.carbs for m in meals), 1),
            "salt": sum_opt(meals, "salt"), "fiber": sum_opt(meals, "fiber"),
            # 食塩・食物繊維の数字がない記録（項目を作る前のもの）がまざっているか
            "partial": any(m.salt is None for m in meals) and any(m.salt is not None for m in meals),
            "count": len(meals)}


# ---- マイメニュー ----
def menu_items(session: Session) -> list[MenuItem]:
    """お気に入りが先で、お気に入りにした順に 固定（食べても動かない。指で場所を覚えられるように）。
    ほかは 最近使ったものが上。"""
    items = session.exec(select(MenuItem)).all()
    favorites = sorted((m for m in items if m.favorite), key=lambda m: (m.favorited_at or m.created_at, m.id))
    others = sorted((m for m in items if not m.favorite),
                    key=lambda m: (m.last_used or m.created_at, m.last_used is not None, m.id), reverse=True)
    return favorites + others


def menu_view(item: MenuItem) -> dict:
    return {"id": item.id, "name": item.name, "kcal": round(item.kcal), "protein": round(item.protein, 1),
            "fat": round(item.fat, 1), "carbs": round(item.carbs, 1),
            "salt": round_opt(item.salt), "fiber": round_opt(item.fiber), "items": json.loads(item.items),
            "use_count": item.use_count, "favorite": item.favorite}


def save_menu(session: Session, name: str, items: list[dict], menu: MenuItem | None = None) -> MenuItem:
    """マイメニューに登録する（menu を渡せば その内容を書きかえる）。"""
    name = name.strip()
    if not name:
        raise HTTPException(400, "マイメニューの名前を入れてね")
    same = session.exec(select(MenuItem).where(MenuItem.name == name)).first()
    if same and (menu is None or same.id != menu.id):
        raise HTTPException(409, "その名前のマイメニューは もうあります")
    if menu is None:
        if len(session.exec(select(MenuItem)).all()) >= config.MENU_MAX:
            raise HTTPException(409, f"マイメニューは{config.MENU_MAX}個までです")
        menu = MenuItem(name=name, created_at=clock.now())
    menu.name = name
    menu.items = json.dumps(items, ensure_ascii=False)
    menu.kcal, menu.protein = sum(i["kcal"] for i in items), sum(i["protein"] for i in items)
    menu.fat, menu.carbs = sum(i["fat"] for i in items), sum(i["carbs"] for i in items)
    menu.salt, menu.fiber = sum_opt(items, "salt"), sum_opt(items, "fiber")
    session.add(menu)
    return menu


def ai_used_today(session: Session) -> int:
    row = session.get(AiUsage, clock.today())
    return row.count if row else 0


@api.get("/meals")
def read_meals(session: Session = Depends(get_session)):
    today = clock.today()
    since = today - timedelta(days=config.FOOD_HISTORY_DAYS - 1)
    meals = session.exec(select(Meal).where(Meal.day >= since).order_by(Meal.eaten_at)).all()
    by_day: dict = {}
    for meal in meals:
        by_day.setdefault(meal.day, []).append(meal)
    return {
        "today": today,
        "meals": [meal_view(m) for m in reversed(by_day.get(today, []))],  # 新しいものが上
        "totals": day_totals(by_day.get(today, [])),
        "energy": energy_today(session, day_totals(by_day.get(today, []))["kcal"]),
        "targets": nutrition_targets(latest_weight(session)),
        "health": health_targets(session),
        "history": [{"day": day, **day_totals(items)} for day, items in sorted(by_day.items(), reverse=True) if day != today],
        "ai": {"available": food_ai.available(), "used": ai_used_today(session), "limit": config.FOOD_AI_DAILY_LIMIT},
        "max_images": config.FOOD_MAX_IMAGES,
        "menu": [menu_view(m) for m in menu_items(session)],
        "servings": config.MENU_SERVINGS,
    }


@api.post("/meals/analyze")
def analyze_meal(body: AnalyzeInput, session: Session = Depends(get_session)):
    """写真や文字から AIに見積もってもらう（まだ記録はしない。画面で確かめて直してから 記録する）。"""
    if not food_ai.available():
        raise HTTPException(503, "AIがまだ設定されていません（OPENAI_API_KEY）。数字を自分で入れてね")
    if not body.images and not body.note.strip():
        raise HTTPException(400, "写真を選ぶか、食べたものを書いてね")
    for image in body.images:
        if not image.startswith(("data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,")):
            raise HTTPException(400, "写真の形がちがいます")
        if len(image) * 3 / 4 > config.FOOD_IMAGE_MAX_BYTES:
            raise HTTPException(400, "写真が大きすぎます")
    usage = session.get(AiUsage, clock.today()) or AiUsage(day=clock.today(), count=0)
    if usage.count >= config.FOOD_AI_DAILY_LIMIT:
        raise HTTPException(429, f"今日は もう{config.FOOD_AI_DAILY_LIMIT}回 AIを使いました。数字を自分で入れてね")
    usage.count += 1  # 失敗しても 数える（何度も呼ばれて お金がかかるのを防ぐ）
    session.add(usage)
    session.commit()

    result = food_ai.estimate(body.images, body.note, list(zip(body.questions, body.answers)))
    if result is None:
        raise HTTPException(502, "AIにつながらなかった。少し待ってから もう一度試してね")
    if result.kind == "not_food":
        raise HTTPException(400, "食べ物が見つからなかった。写真を撮り直すか、食べたものを書いてね")
    return {
        "kind": result.kind,
        "items": [item.model_dump() for item in result.items],
        "questions": result.questions,
        "comment": result.comment,
        "ai": {"available": True, "used": usage.count, "limit": config.FOOD_AI_DAILY_LIMIT},
    }


class EstimateItemInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    amount: str = Field(default="", max_length=40)
    context: str = Field(default="", max_length=600)  # いっしょに食べたもの・ひとこと（ヒント）


@api.post("/meals/estimate-item")
def estimate_item(body: EstimateItemInput, session: Session = Depends(get_session)):
    """品目を1つだけ AIに計算してもらう（品目を足したとき・名前や量を変えたとき）。"""
    if not food_ai.available():
        raise HTTPException(503, "AIがまだ設定されていません。数字を自分で入れてね")
    usage = session.get(AiUsage, clock.today()) or AiUsage(day=clock.today(), count=0)
    if usage.item_count >= config.FOOD_ITEM_AI_DAILY_LIMIT:
        raise HTTPException(429, f"今日は もう{config.FOOD_ITEM_AI_DAILY_LIMIT}回 計算しました。数字を自分で入れてね")
    usage.item_count += 1  # 失敗しても 数える
    session.add(usage)
    session.commit()
    item = food_ai.estimate_item(body.name, body.amount, body.context)
    if item is None:
        raise HTTPException(502, "AIにつながらなかった。数字を自分で入れてね")
    return item.model_dump()


@api.post("/meals")
def create_meal(body: MealInput, session: Session = Depends(get_session)):
    now = clock.now()
    eaten = naive(body.eaten_at) or now
    if eaten > now + timedelta(minutes=5):
        raise HTTPException(400, "これからの時刻は選べません")
    items = [item.model_dump() for item in body.items]
    meal = Meal(
        day=clock.game_day(eaten), eaten_at=eaten, title="、".join(i["name"] for i in items)[:80],
        kcal=sum(i["kcal"] for i in items), protein=sum(i["protein"] for i in items),
        fat=sum(i["fat"] for i in items), carbs=sum(i["carbs"] for i in items),
        salt=sum_opt(items, "salt"), fiber=sum_opt(items, "fiber"),
        items=json.dumps(items, ensure_ascii=False), note=body.note.strip() or None, source=body.source, created_at=now,
    )
    session.add(meal)
    if body.menu_name and body.menu_name.strip():  # マイメニューにも登録する
        save_menu(session, body.menu_name, items)
    session.commit()
    session.refresh(meal)
    return meal_view(meal)


@api.post("/menu")
def create_menu(body: MenuInput, session: Session = Depends(get_session)):
    menu = save_menu(session, body.name, [item.model_dump() for item in body.items])
    session.commit()
    session.refresh(menu)
    return menu_view(menu)


@api.post("/menu/from-meal/{meal_id}")
def create_menu_from_meal(meal_id: int, body: MenuFromMealInput, session: Session = Depends(get_session)):
    """記録した食事を、そのままマイメニューに登録する。"""
    meal = session.get(Meal, meal_id)
    if meal is None:
        raise HTTPException(404, "その食事の記録は見つかりません")
    menu = save_menu(session, body.name, json.loads(meal.items))
    session.commit()
    session.refresh(menu)
    return menu_view(menu)


@api.put("/menu/{menu_id}")
def update_menu(menu_id: int, body: MenuInput, session: Session = Depends(get_session)):
    menu = session.get(MenuItem, menu_id)
    if menu is None:
        raise HTTPException(404, "そのマイメニューは見つかりません")
    save_menu(session, body.name, [item.model_dump() for item in body.items], menu)
    session.commit()
    session.refresh(menu)
    return menu_view(menu)


@api.put("/menu/{menu_id}/favorite")
def favorite_menu(menu_id: int, body: FavoriteInput, session: Session = Depends(get_session)):
    """お気に入りにする・外す。"""
    menu = session.get(MenuItem, menu_id)
    if menu is None:
        raise HTTPException(404, "そのマイメニューは見つかりません")
    if body.favorite and not menu.favorite:
        menu.favorited_at = clock.now()
    menu.favorite = body.favorite
    if not body.favorite:
        menu.favorited_at = None
    session.commit()
    session.refresh(menu)
    return menu_view(menu)


@api.delete("/menu/{menu_id}")
def delete_menu(menu_id: int, session: Session = Depends(get_session)):
    menu = session.get(MenuItem, menu_id)
    if menu is None:
        raise HTTPException(404, "そのマイメニューは見つかりません")
    session.delete(menu)
    session.commit()
    return {"ok": True}


@api.post("/menu/{menu_id}/eat")
def eat_menu(menu_id: int, body: EatInput, session: Session = Depends(get_session)):
    """マイメニューを食べた：登録した数字で 食事を記録する（AIは使わない）。"""
    menu = session.get(MenuItem, menu_id)
    if menu is None:
        raise HTTPException(404, "そのマイメニューは見つかりません")
    now = clock.now()
    n = body.servings
    scale = lambda v: None if v is None else round(v * n, 1)  # noqa: E731（食塩・食物繊維は 前の登録だと ない）
    items = [{**i, "kcal": round(i["kcal"] * n, 1), "protein": round(i["protein"] * n, 1), "fat": round(i["fat"] * n, 1),
              "carbs": round(i["carbs"] * n, 1), "salt": scale(i.get("salt")), "fiber": scale(i.get("fiber")),
              "amount": f"{i.get('amount') or ''}×{n:g}".strip() if n != 1 else i.get("amount", "")}
             for i in json.loads(menu.items)]
    title = menu.name if n == 1 else f"{menu.name}×{n:g}"
    meal = Meal(day=clock.game_day(now), eaten_at=now, title=title[:80], kcal=sum(i["kcal"] for i in items),
                protein=sum(i["protein"] for i in items), fat=sum(i["fat"] for i in items),
                carbs=sum(i["carbs"] for i in items), salt=sum_opt(items, "salt"), fiber=sum_opt(items, "fiber"),
                items=json.dumps(items, ensure_ascii=False), source="menu",
                created_at=now)
    menu.use_count += 1
    menu.last_used = now
    session.add(meal)
    session.commit()
    session.refresh(meal)
    return meal_view(meal)


@api.delete("/meals/{meal_id}")
def delete_meal(meal_id: int, session: Session = Depends(get_session)):
    meal = session.get(Meal, meal_id)
    if meal is None:
        raise HTTPException(404, "その食事の記録は見つかりません")
    session.delete(meal)
    session.commit()
    return {"ok": True}


# ---- ハビットモンスターが 取りに来る API（取りこみ用の合言葉が必要）----
sync = APIRouter(prefix="/api/sync", dependencies=[Depends(auth.require_sync)])


def export_workout(session: Session, w: Workout) -> dict:
    rows = rows_of(session, w.id)
    warmups = json.loads(w.warmups) if w.warmups else {}
    names = {str(ex.id): ex.name for _, ex in rows}
    return {
        "id": w.id, "source_id": w.source_id, "day": w.day, "started_at": w.started_at, "ended_at": w.ended_at,
        "minutes": w.minutes, "body_weight": w.body_weight, "body_fat": w.body_fat, "memo": w.memo,
        "warmups": {names[k]: n for k, n in warmups.items() if k in names},
        "sets": [{"exercise": ex.name, "weight": s.weight, "reps": s.reps, "duration_min": s.duration_min,
                  "distance_km": s.distance_km, "created_at": s.created_at} for s, ex in rows],
    }


@sync.get("/workouts")
def sync_workouts(since: str | None = None, session: Session = Depends(get_session)):
    """終わった筋トレ（since の日から。なければ ぜんぶ）と、種目の一覧。"""
    query = select(Workout).where(Workout.status == "done").order_by(Workout.started_at)
    if since:
        query = query.where(Workout.day >= datetime.fromisoformat(since).date())
    exercises = session.exec(select(Exercise).order_by(Exercise.id)).all()
    return {
        "workouts": [export_workout(session, w) for w in session.exec(query).all()],
        "exercises": [{"name": e.name, "part": e.part, "mets": e.mets, "mets_estimated": e.mets_estimated,
                       "warmup_sets": e.warmup_sets, "preset": e.preset} for e in exercises],
    }


@sync.get("/recent")
def sync_recent(days: int = 7, session: Session = Depends(get_session)):
    """最近の筋トレ（記録中のものも）の ようす。記録が消えたときなどに 確かめる用（読むだけ）。"""
    since = clock.today() - timedelta(days=max(1, min(days, 60)))
    rows = session.exec(select(Workout).where(Workout.day >= since).order_by(Workout.id)).all()
    return {"workouts": [{"id": w.id, "day": w.day, "status": w.status, "started_at": w.started_at, "ended_at": w.ended_at,
                          "menu_id": w.menu_id, "sets": len(rows_of(session, w.id))} for w in rows],
            "menus": len(session.exec(select(WorkoutMenu)).all()),
            # 最後に作られた番号（消えた記録が あったかを 見分ける。PostgreSQL のときだけ）
            "last_ids": last_ids(session)}


def last_ids(session: Session) -> dict:
    from sqlalchemy import text

    if session.get_bind().dialect.name != "postgresql":
        return {}
    result = {}
    for table in ("workout", "workoutset", "meal"):
        try:
            result[table] = session.exec(text(f"SELECT last_value FROM {table}_id_seq")).one()[0]
        except Exception:
            session.rollback()
    return result


class ImportExercise(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    part: PartKey
    mets: float | None = None
    warmup_sets: int = Field(default=0, ge=0, le=config.WARMUP_MAX_SETS)


class ImportSet(BaseModel):
    exercise: str
    weight: float | None = None
    reps: int | None = None
    duration_min: float | None = None
    distance_km: float | None = None
    is_pr: bool = False
    created_at: datetime | None = None


class ImportWorkout(BaseModel):
    source_id: int
    day: str
    started_at: datetime
    ended_at: datetime
    minutes: int
    body_weight: float | None = None
    body_fat: float | None = None
    memo: str | None = None
    warmups: dict[str, int] = {}
    sets: list[ImportSet]


class ImportInput(BaseModel):
    exercises: list[ImportExercise] = []
    workouts: list[ImportWorkout] = []


@sync.post("/import")
def sync_import(body: ImportInput, session: Session = Depends(get_session)):
    """ハビモンにある 今までの記録を 引っ越す。同じ記録（source_id）は 2回入れない。"""
    by_name = {e.name: e for e in session.exec(select(Exercise)).all()}
    for item in body.exercises:
        ex = by_name.get(item.name)
        if ex is None:
            ex = Exercise(name=item.name, part=item.part)
            session.add(ex)
            by_name[item.name] = ex
        if item.mets:
            ex.mets, ex.mets_estimated = item.mets, True
        ex.warmup_sets = item.warmup_sets
    session.flush()

    mapping = {}
    for item in body.workouts:
        existing = session.exec(select(Workout).where(Workout.source_id == item.source_id)).first()
        if existing:
            mapping[str(item.source_id)] = existing.id
            continue
        missing = [s.exercise for s in item.sets if s.exercise not in by_name]
        if missing:
            raise HTTPException(400, f"種目が登録されていません：{missing[0]}")
        w = Workout(day=datetime.fromisoformat(item.day).date(), started_at=naive(item.started_at),
                    ended_at=naive(item.ended_at), minutes=item.minutes, status="done", body_weight=item.body_weight,
                    body_fat=item.body_fat, memo=item.memo, source_id=item.source_id,
                    warmups=json.dumps({str(by_name[name].id): n for name, n in item.warmups.items() if name in by_name}))
        session.add(w)
        session.flush()
        for s in item.sets:
            session.add(WorkoutSet(workout_id=w.id, exercise_id=by_name[s.exercise].id, weight=s.weight, reps=s.reps,
                                   duration_min=s.duration_min, distance_km=s.distance_km, is_pr=s.is_pr,
                                   created_at=naive(s.created_at) or w.started_at))
        mapping[str(item.source_id)] = w.id
    session.commit()
    return {"workouts": mapping, "exercise_count": len(by_name)}


@sync.get("/meals")
def sync_meals(since: str | None = None, session: Session = Depends(get_session)):
    """食事の記録（since の日から。なければ ぜんぶ）。"""
    query = select(Meal).order_by(Meal.eaten_at)
    if since:
        query = query.where(Meal.day >= datetime.fromisoformat(since).date())
    return {"meals": [meal_view(m) for m in session.exec(query).all()]}


@sync.delete("/workouts/{workout_id}")
def sync_delete(workout_id: int, session: Session = Depends(get_session)):
    """終わった筋トレを消す（ハビモンで記録を取り消したとき）。"""
    w = session.get(Workout, workout_id)
    if w is None:
        return {"deleted": False}  # もう ない
    for s in session.exec(select(WorkoutSet).where(WorkoutSet.workout_id == w.id)).all():
        session.delete(s)
    session.flush()  # 先にセットを消してから 本体を消す（PostgreSQL は 順番がちがうと 断る）
    session.delete(w)
    session.commit()
    return {"deleted": True}


class ProfileInput(BaseModel):
    height: float | None = Field(default=None, gt=50, lt=260)
    birth_date: str | None = None  # 例 2004-04-01
    sex: Literal["male", "female", "other"] | None = None


@sync.put("/profile")
def sync_profile(body: ProfileInput, session: Session = Depends(get_session)):
    """ハビットモンスターのプロフィール（身長・生年月日・性別）を 覚える。基礎代謝の計算に使う。"""
    if body.birth_date:
        try:
            datetime.fromisoformat(body.birth_date)
        except ValueError as err:
            raise HTTPException(400, "生年月日の形がちがいます") from err
    value = json.dumps(body.model_dump(), ensure_ascii=False)
    setting = session.get(AppSetting, PROFILE_KEY)
    if setting is None:
        session.add(AppSetting(key=PROFILE_KEY, value=value))
    else:
        setting.value = value
    session.commit()
    return {"ok": True}


PARTNER_KEY = "partner"  # ハビットモンスターの相棒（名前・ランク・レベル・画像）。見るだけ


class PartnerCommentInput(BaseModel):
    id: int
    kind: Literal["day", "week"]
    label: str = Field(max_length=60)
    text: str = Field(max_length=400)


class PartnerInput(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    rank: str = Field(min_length=1, max_length=2)
    level: int = Field(ge=1, le=999)
    motion: Literal["hop", "squish", "float", "spin", "sway", "shiver", "egg"] = "hop"  # なでたときの動き
    lines: list[str] | None = Field(default=None, max_length=8)  # 休憩おわりのセリフ（キャラごとに ハビモンが作る）
    comments: list[PartnerCommentInput] | None = Field(default=None, max_length=4)  # 相棒からのコメント（きのう・先週）
    image: str | None = Field(default=None, max_length=800_000)  # WebP を base64 にした文字


@sync.put("/partner")
def sync_partner(body: PartnerInput, session: Session = Depends(get_session)):
    """ハビットモンスターの相棒を 覚える（画面のいちばん上に出す）。"""
    version = None
    if body.image:
        try:
            base64.b64decode(body.image, validate=True)
        except ValueError as err:
            raise HTTPException(400, "画像の形がちがいます") from err
        version = hashlib.sha1(body.image.encode()).hexdigest()[:12]
    before = get_partner(session) or {}
    value = json.dumps({**body.model_dump(), "version": version}, ensure_ascii=False)
    # 新しいコメントが届いたら 通知で知らせる（初めて相棒が届いたときは 知らせない）
    seen = {c["id"] for c in before.get("comments") or []}
    fresh = [c for c in body.comments or [] if c.id not in seen]
    if before and fresh and push.configured() and push.subscriptions(session):
        push.notify_now({"title": body.name, "body": f"{body.name}からメッセージが届いたよ（{fresh[0].label}）", "tag": "comment"})
    setting = session.get(AppSetting, PARTNER_KEY)
    if setting is None:
        session.add(AppSetting(key=PARTNER_KEY, value=value))
    else:
        setting.value = value
    session.commit()
    return {"ok": True}


class MetsInput(BaseModel):
    mets: dict[str, float]  # 種目の名前 → 強さ（ハビモンが AIで決めた値）


@sync.put("/exercises/mets")
def sync_mets(body: MetsInput, session: Session = Depends(get_session)):
    updated = 0
    for ex in session.exec(select(Exercise)).all():
        if ex.name in body.mets and 1 <= body.mets[ex.name] <= 20:
            ex.mets, ex.mets_estimated = body.mets[ex.name], True
            updated += 1
    session.commit()
    return {"updated": updated}


# ---- アプリ本体 ----
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with Session(engine) as session:
        ensure_presets(session)
    yield


app = FastAPI(title=config.APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/api/login")
def login(body: LoginInput, request: Request, response: Response):
    auth.check_password(request, body.password)
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(auth.COOKIE, auth.make_token(), max_age=config.LOGIN_DAYS * 86400, httponly=True,
                        secure=secure, samesite="lax")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(auth.require_login)])
def me():
    return {"ok": True, "name": config.APP_NAME}


@app.get("/healthz")
def healthz():
    """動いているか と、どの版か（Render が入れてくれる コミットの番号の 先頭7文字）。入れ替わったかの確認に使う。"""
    return {"ok": True, "version": (os.getenv("RENDER_GIT_COMMIT") or "local")[:7]}


app.include_router(api)
app.include_router(sync)


class NoCacheStaticFiles(StaticFiles):
    """画面のファイルは 毎回 新しくなっていないか 確かめさせる。"""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")
