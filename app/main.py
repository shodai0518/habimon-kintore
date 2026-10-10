"""ハビモン筋トレ：筋トレの記録だけをする 小さなアプリ。

スマホで記録 → ハビットモンスターが /api/sync から取りこむ → ハビモンで 経験値などがもらえる。
起動（パソコンで試すとき）: KINTORE_DEV=1 python -m uvicorn app.main:app --port 8100
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import auth, calorie, clock, config, rules
from app.db import engine, get_session, init_db
from app.models import Exercise, Workout, WorkoutSet

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PartKey = Literal["chest", "shoulders", "arms", "back", "abs", "legs", "cardio"]


# ---- 入力 ----
class LoginInput(BaseModel):
    password: str = Field(max_length=200)


class StartInput(BaseModel):
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
        minutes = w.minutes
    else:  # 記録中：あとから記録なら 入れた終了時刻まで、今やっているなら 今まで
        end = w.planned_end or clock.now()
        minutes = min(config.WORKOUT_MAX_MINUTES, max(0, (end - w.started_at).total_seconds() / 60))
    sets = [{"name": ex.name, "mets": ex.mets, "cardio": ex.part == "cardio",
             "duration_min": s.duration_min, "distance_km": s.distance_km} for s, ex in rows]
    warmups = json.loads(w.warmups) if w.warmups else {}
    names = {str(ex.id): ex.name for _, ex in rows}
    weight = weight_for(session, w)
    result = calorie.workout_calories(minutes, sets, weight, {names[k]: n for k, n in warmups.items() if k in names})
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
    }


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


@api.get("/workouts")
def read_workouts(session: Session = Depends(get_session)):
    since = clock.today() - timedelta(days=config.HISTORY_DAYS - 1)
    active = get_active(session)
    done = session.exec(
        select(Workout).where(Workout.status == "done", Workout.day >= since).order_by(Workout.started_at.desc())
    ).all()
    last_weight = session.exec(
        select(Workout).where(Workout.body_weight.is_not(None)).order_by(Workout.started_at.desc())
    ).first()
    return {
        "current": workout_view(session, active) if active else None,
        "history": [workout_view(session, w) for w in done],
        "last_weight": last_weight.body_weight if last_weight else None,
        "last_fat": last_weight.body_fat if last_weight else None,
        "backfill_days": config.WORKOUT_BACKFILL_DAYS,
        "now": clock.now(),
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
    w = Workout(day=clock.game_day(started), started_at=started,
                planned_end=planned_end if planned_end and planned_end > started else None,
                body_weight=body.body_weight, body_fat=body.body_fat)
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
    """記録中の筋トレを やめる（記録は残らない）。"""
    w = load(session, workout_id, "active")
    for s in session.exec(select(WorkoutSet).where(WorkoutSet.workout_id == w.id)).all():
        session.delete(s)
    session.flush()  # 先にセットを消してから 本体を消す（PostgreSQL は 順番がちがうと 断る）
    session.delete(w)
    session.commit()
    return {"ok": True}


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
    return {"ok": True}


app.include_router(api)
app.include_router(sync)


class NoCacheStaticFiles(StaticFiles):
    """画面のファイルは 毎回 新しくなっていないか 確かめさせる。"""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")
