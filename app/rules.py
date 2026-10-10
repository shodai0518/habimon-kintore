"""筋トレの ルール（自己ベスト・セットの表示）。ハビットモンスターと 同じ。"""


def one_rep_max(weight: float | None, reps: int | None) -> float:
    """1回だけ持ち上げられる重さの 見つもり（Epley の式）。自重なら 回数で比べる。"""
    if not reps:
        return 0.0
    if not weight:
        return float(reps) / 100  # 自重の種目は 回数が多いほど 上（重さのある記録よりは下）
    return weight * (1 + reps / 30)


def best_set(sets: list) -> object | None:
    """いちばん強いセット（1RMの見つもりが いちばん大きい）。"""
    scored = [s for s in sets if s.reps]
    return max(scored, key=lambda s: one_rep_max(s.weight, s.reps), default=None)


def set_label(s) -> str:
    if s.duration_min is not None or s.distance_km is not None:
        parts = []
        if s.duration_min:
            parts.append(f"{s.duration_min:g}分")
        if s.distance_km:
            parts.append(f"{s.distance_km:g}km")
        return "・".join(parts) or "記録なし"
    weight = f"{s.weight:g}kg×" if s.weight else "自重×"
    return f"{weight}{s.reps}回"
