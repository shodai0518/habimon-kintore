# ハビモン筋トレ

筋トレの記録だけをする、小さなアプリ。スマホで記録して、パソコンのハビットモンスターが取りこむ。

```
[スマホ] ハビモン筋トレ（インターネット上）──▶ [データベース（Neon）]
                                                     ▲
[パソコン] ハビットモンスター ──── 取りに行く ────────┘  → 経験値などのご褒美
```

- 記録できるもの：種目・重さ・回数、有酸素の時間と距離、本番の前のアップのセット数、体重・体脂肪率、メモ
- 記録するのは このアプリだけ。ハビットモンスターは 取りこんだ記録とグラフを見る場所
- 経験値・ステータス・ゴールドは、ハビットモンスターが取りこんだときにもらえる（筋トレをした日の分として数える）

## パソコンの中で試す

ハビットモンスターのフォルダで（.venv を使う）:

```
cd kintore_app
set KINTORE_DEV=1
..\.venv\Scripts\python -m uvicorn app.main:app --port 8100
```

http://localhost:8100 を開く。`KINTORE_DEV=1` は ログインなしで使える 試し用の設定（**インターネット上では絶対に付けない**）。
データは `kintore_app/data/kintore.db` に入る。

テスト:

```
cd kintore_app
..\.venv\Scripts\python -m pytest -q tests
```

## インターネット上に置く（Render ＋ Neon、どちらも無料）

設定する値（Render の Environment に入れる）:

| 名前 | 中身 |
|---|---|
| `DATABASE_URL` | Neon の接続文字列（`postgresql://...` で始まる） |
| `APP_PASSWORD` | スマホで開くときの合言葉（自分で決める） |
| `SYNC_TOKEN` | ハビットモンスターが取りに来るときの合言葉（長い でたらめな文字） |
| `SECRET_KEY` | ログインの印にサインする鍵（長い でたらめな文字。render.yaml を使えば自動で作られる） |
| `APP_TIMEZONE` | `Asia/Tokyo`（サーバーが外国にあっても 日本時間で記録する） |
| `PYTHON_VERSION` | `3.13.5` |

- Build Command：`pip install -r requirements.txt`
- Start Command：`uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health Check Path：`/healthz`

無料プランは 15分使わないと休む。次に開くとき、起きるまで 30秒〜1分かかる。

## ハビットモンスター側の設定

ハビットモンスターの `.env` に 2行足す:

```
WORKOUT_APP_URL=https://（Render がくれた名前）.onrender.com
WORKOUT_APP_TOKEN=（SYNC_TOKEN と同じ文字）
```

これを入れると、ハビットモンスターの筋トレ画面が「筋トレアプリで記録してね／取りこむ」に変わる。
筋トレ画面を開いたときに 自動で取りに行く。

今までの記録の引っ越し（1回だけ）: ハビットモンスターを起動して、次を実行する。

```
curl -X POST http://localhost:8000/api/workouts/push-history
```

## 中身

| 場所 | 中身 |
|---|---|
| `app/main.py` | API（記録・ログイン・取りこみ用） |
| `app/auth.py` | ログイン（合言葉、まちがいが続くと待たせる） |
| `app/models.py` | データベースのテーブル（種目・筋トレ・セット） |
| `app/calorie.py` `app/rules.py` `app/config.py` | カロリーの計算・自己ベスト・決まりごと（ハビットモンスターと同じ） |
| `static/` | 画面（HTML / CSS / JavaScript） |
| `tests/` | テスト |

## 気をつけること

- 新しく足した種目の強さ（METs）は、最初は仮の値。ハビットモンスターが取りこむときに AI で決めて、このアプリにも書きもどす
- 終わった筋トレは あとから直せない
- `.env` や合言葉は GitHub に上げない（`.gitignore` に入れてある）
