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
    """筋トレの 本番1セットにかかる時間の 決まった長さ（動いている時間 ＋ 休憩）。
    実際の時間が分からないとき（あとから記録・まとめて入力）に使う。"""
    rest = config.CALORIE_BIG3_REST_MINUTES if exercise_name in config.BIG3_EXERCISES else config.CALORIE_REST_MINUTES
    return config.CALORIE_SET_WORK_MINUTES + rest


def strength_minutes(sets: list[dict], warmups: dict[str, int], started_at=None, ended_at=None) -> tuple[list[float], set[str]]:
    """筋トレの セットごとの時間（分）と、アップの時間を もう数えた種目。有酸素のセットは 0。

    セットを追加した時刻（at）が分かるときは、「前のセットを追加してから このセットを追加するまで」を
    そのセットの時間（休憩 ＋ 動いている時間）として使う。席を外した時間まで数えないように 上限をつける。
    種目の最初のセットは、その前に アップのセットをやっているので、そのぶん 上限をのばす（アップは この中に入る）。
    時刻が当てにならないとき（あとから記録・まとめて入力・時刻なし）は、決まった長さで数える。
    """
    warmup_set = config.CALORIE_SET_WORK_MINUTES + config.CALORIE_WARMUP_REST_MINUTES
    minutes = [0.0] * len(sets)
    warmup_counted: set[str] = set()
    seen: set[str] = set()
    previous = started_at
    in_window = lambda t: (t is not None and started_at is not None and ended_at is not None
                           and started_at <= t <= ended_at + config.CALORIE_LOG_MARGIN)
    order = sorted(range(len(sets)), key=lambda i: (sets[i].get("at") is None, sets[i].get("at") or 0, i))
    for i in order:
        s = sets[i]
        at = s.get("at")
        if s["cardio"]:
            previous = at if in_window(at) else previous
            continue
        first = s["name"] not in seen
        seen.add(s["name"])
        gap = (at - previous).total_seconds() / 60 if in_window(at) and in_window(previous) and at >= previous else None
        if gap is None or gap < config.CALORIE_BATCH_MINUTES:  # 時刻が当てにならない → 決まった長さ
            minutes[i] = set_minutes(s["name"])
        else:
            limit = config.CALORIE_MAX_SET_MINUTES
            if first and warmups.get(s["name"], 0):
                limit += warmups[s["name"]] * warmup_set
                warmup_counted.add(s["name"])  # アップの時間は この中に入っている
            minutes[i] = min(gap, limit)
        if in_window(at):
            previous = at
    return minutes, warmup_counted


def workout_calories(total_minutes: float, sets: list[dict], weight: float | None,
                     warmups: dict[str, int] | None = None, started_at=None, ended_at=None) -> dict:
    """1回の筋トレの 消費カロリー。

    sets：1セットごとに {name, mets, cardio, duration_min, distance_km, at（追加した時刻。なくてもよい）}
    warmups：種目の名前 → アップのセット数
    started_at / ended_at：筋トレの開始と終了（記録中なら 今）。あれば、実際の休憩時間で数える。
    有酸素は 記録した時間。筋トレの種目は セットごとの時間（strength_minutes）＋ まだ数えていないアップ。
    ぜんぶ足して 筋トレ全体の時間（total_minutes）を超えるときは、収まるように縮める。
    """
    if not weight or not sets:
        return {"total": None, "by_exercise": {}}
    warmups = warmups or {}
    cardio_minutes = sum(s.get("duration_min") or 0 for s in sets if s["cardio"])
    per_set, warmup_counted = strength_minutes(sets, warmups, started_at, ended_at)
    strength_names = {s["name"] for s in sets if not s["cardio"]}
    warmup_minutes = {name: warmups.get(name, 0) * (config.CALORIE_SET_WORK_MINUTES + config.CALORIE_WARMUP_REST_MINUTES)
                      for name in strength_names if name not in warmup_counted}
    strength_total = sum(per_set) + sum(warmup_minutes.values())
    available = max(0.0, total_minutes - cardio_minutes)
    scale = min(1.0, available / strength_total) if strength_total else 0

    by_exercise: dict[str, float] = {}
    for s, set_time in zip(sets, per_set):
        minutes = (s.get("duration_min") or 0) if s["cardio"] else set_time * scale
        mets = set_mets(s["name"], s["mets"], s["cardio"], s.get("duration_min"), s.get("distance_km"))
        by_exercise[s["name"]] = by_exercise.get(s["name"], 0) + kcal(mets, weight, minutes)
        if not s["cardio"] and warmup_minutes.get(s["name"]):  # アップは 種目ごとに1回だけ足す
            by_exercise[s["name"]] += kcal(mets, weight, warmup_minutes.pop(s["name"]) * scale)
    by_exercise = {name: round(value) for name, value in by_exercise.items()}
    return {"total": sum(by_exercise.values()), "by_exercise": by_exercise}
