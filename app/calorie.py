"""消費カロリーの計算（目安）。ハビットモンスターと 同じ計算。"""

from app import config


def interpolate(table: list[tuple[float, float]], x: float) -> float:
    """表の あいだの値を 直線でおぎなう（表の外は 端の値）。"""
    if x <= table[0][0]:
        return table[0][1]
    for (x1, y1), (x2, y2) in zip(table, table[1:]):
        if x <= x2:
            return y1 + (y2 - y1) * (x - x1) / (x2 - x1)
    return table[-1][1]


def set_mets(exercise_name: str, exercise_mets: float | None, is_cardio: bool,
             duration_min: float | None = None, distance_km: float | None = None) -> float:
    """1セットの METs。ランニングなどは 速さで決める。"""
    kind = config.SPEED_METS_EXERCISES.get(exercise_name)
    if kind and duration_min and distance_km:
        return interpolate(config.SPEED_METS[kind], distance_km / (duration_min / 60))
    if exercise_mets:
        return exercise_mets
    return config.DEFAULT_CARDIO_METS if is_cardio else config.DEFAULT_METS


def kcal(mets: float, weight: float, minutes: float) -> float:
    return mets * weight * minutes / 60 * config.CALORIE_FACTOR


def set_minutes(exercise_name: str) -> float:
    """筋トレの 本番1セットにかかる時間（動いている時間 ＋ 休憩）。"""
    rest = config.CALORIE_BIG3_REST_MINUTES if exercise_name in config.BIG3_EXERCISES else config.CALORIE_REST_MINUTES
    return config.CALORIE_SET_WORK_MINUTES + rest


def workout_calories(total_minutes: float, sets: list[dict], weight: float | None,
                     warmups: dict[str, int] | None = None) -> dict:
    """1回の筋トレの 消費カロリー。

    sets：1セットごとに {name, mets, cardio, duration_min, distance_km}
    warmups：種目の名前 → アップのセット数
    有酸素は 記録した時間。筋トレの種目は「1セットにかかる時間 × セット数」＋ アップのセット。
    ぜんぶ足して 筋トレ全体の時間（total_minutes）を超えるときは、収まるように縮める。
    """
    if not weight or not sets:
        return {"total": None, "by_exercise": {}}
    warmups = warmups or {}
    cardio_minutes = sum(s.get("duration_min") or 0 for s in sets if s["cardio"])
    strength_names = {s["name"] for s in sets if not s["cardio"]}
    warmup_minutes = {name: warmups.get(name, 0) * (config.CALORIE_SET_WORK_MINUTES + config.CALORIE_WARMUP_REST_MINUTES)
                      for name in strength_names}
    strength_total = sum(set_minutes(s["name"]) for s in sets if not s["cardio"]) + sum(warmup_minutes.values())
    available = max(0.0, total_minutes - cardio_minutes)
    scale = min(1.0, available / strength_total) if strength_total else 0

    by_exercise: dict[str, float] = {}
    for s in sets:
        minutes = (s.get("duration_min") or 0) if s["cardio"] else set_minutes(s["name"]) * scale
        mets = set_mets(s["name"], s["mets"], s["cardio"], s.get("duration_min"), s.get("distance_km"))
        by_exercise[s["name"]] = by_exercise.get(s["name"], 0) + kcal(mets, weight, minutes)
        if not s["cardio"] and warmup_minutes.get(s["name"]):  # アップは 種目ごとに1回だけ足す
            by_exercise[s["name"]] += kcal(mets, weight, warmup_minutes.pop(s["name"]) * scale)
    by_exercise = {name: round(value) for name, value in by_exercise.items()}
    return {"total": sum(by_exercise.values()), "by_exercise": by_exercise}
