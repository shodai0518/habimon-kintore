from datetime import date, datetime

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel


class Exercise(SQLModel, table=True):
    """筋トレの種目"""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    part: str  # config.BODY_PARTS のキー（cardio なら 有酸素）
    mets: float | None = None  # 運動の強さ（消費カロリーの計算に使う）
    mets_estimated: bool = True  # False：仮の値（ハビモンが取りこむときに AIで決め直す）
    warmup_sets: int = 0  # いつもやる アップのセット数（前に選んだ数を覚えておく）
    rest_seconds: int | None = None  # セットの間の休憩（秒）。自分で変えた種目だけ入る（None なら 決まった長さ）
    preset: bool = False  # 最初から用意した種目か
    created_at: NaiveDatetime = Field(default_factory=datetime.now)


class Workout(SQLModel, table=True):
    """1回の筋トレ"""

    id: int | None = Field(default=None, primary_key=True)
    day: date
    started_at: NaiveDatetime
    ended_at: NaiveDatetime | None = None
    planned_end: NaiveDatetime | None = None  # あとから記録するときの 終了時刻
    status: str = "active"  # active（記録中） / done
    body_weight: float | None = None  # 筋トレ前の体重（kg）
    body_fat: float | None = None  # 体脂肪率（%）
    memo: str | None = None
    minutes: int | None = None
    warmups: str | None = None  # 種目ごとの アップのセット数（JSON：種目のid → 数）
    source_id: int | None = None  # ハビモンから引っ越してきた記録の、ハビモンでの番号


class WorkoutSet(SQLModel, table=True):
    """1セット（有酸素なら 1回ぶん）"""

    id: int | None = Field(default=None, primary_key=True)
    workout_id: int = Field(foreign_key="workout.id", index=True)
    exercise_id: int = Field(foreign_key="exercise.id", index=True)
    weight: float | None = None  # kg（自重なら なし）
    reps: int | None = None
    duration_min: float | None = None  # 有酸素：時間（分）
    distance_km: float | None = None  # 有酸素：距離（km）
    is_pr: bool = False  # 自己ベスト更新
    created_at: NaiveDatetime = Field(default_factory=datetime.now)


class Meal(SQLModel, table=True):
    """1回の食事（写真は保存しない。数字だけ）"""

    id: int | None = Field(default=None, primary_key=True)
    day: date = Field(index=True)  # 何日の分か（朝4時区切り）
    eaten_at: NaiveDatetime
    title: str  # 品目の名前をつなげたもの（一覧に出す）
    kcal: float = 0
    protein: float = 0  # g
    fat: float = 0  # g
    carbs: float = 0  # g
    items: str = "[]"  # 品目ごとの内訳（JSON）
    note: str | None = None  # 自分で書いた ひとこと
    source: str = "manual"  # photo（写真）/ text（文字だけ）/ label（栄養成分表示）/ manual（自分で入力）
    created_at: NaiveDatetime = Field(default_factory=datetime.now)


class AiUsage(SQLModel, table=True):
    """AIを使った回数（1日の上限を数える）"""

    day: date = Field(primary_key=True)
    count: int = 0


class MenuItem(SQLModel, table=True):
    """マイメニュー：よく食べるものを、正しい数字と一緒に登録しておく（AIを使わずに 選ぶだけで記録できる）"""

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    kcal: float = 0  # 1個（1人前）ぶん
    protein: float = 0
    fat: float = 0
    carbs: float = 0
    items: str = "[]"  # 品目ごとの内訳（JSON）
    use_count: int = 0
    favorite: bool = False  # お気に入り（いつも見えるところに出す）
    last_used: NaiveDatetime | None = None
    created_at: NaiveDatetime = Field(default_factory=datetime.now)


class AppSetting(SQLModel, table=True):
    """アプリの設定（プロフィールなど）。key ごとに 1つの値（JSON）"""

    key: str = Field(primary_key=True)
    value: str
