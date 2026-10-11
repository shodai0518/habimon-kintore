# ハビモン筋トレ：スマホで使えるようにする手順

筋トレアプリをインターネット上に置いて、スマホで開けるようにするまでの手順。
上から順番に進める。終わったところに `[x]` を付けていく。

かかるお金：0円（GitHub・Neon・Render の無料プランだけを使う）

---

## 0. 全体の流れ

| 順番 | やること | 使うサービス |
|---|---|---|
| 1 | プログラムの置き場所を作る | GitHub |
| 2 | データベースを作る | Neon |
| 3 | 合言葉を2つ決める | （自分で決める） |
| 4 | アプリを動かす | Render |
| 5 | ハビットモンスターとつなぐ | パソコンの `.env` |
| 6 | 今までの記録を引っ越す | パソコン |
| 7 | スマホのホーム画面に追加する | iPhone |

---

## 1. GitHub：空のリポジトリを作る

- [ ] https://github.com/new を開く
- [ ] Repository name：`habimon-kintore`
- [ ] **Private**（非公開）を選ぶ
- [ ] 「Add a README file」「Add .gitignore」「Choose a license」は **全部外したまま**
- [ ] 「Create repository」を押す
- [ ] できたページの URL を控える

```
控え：https://github.com/＿＿＿＿＿＿/habimon-kintore
```

このあと、`kintore_app` フォルダの中身をここに上げる（Claude に URL を伝えれば、上げるところまでやってもらえる）。

---

## 2. Neon：データベースを作る

- [ ] https://neon.tech を開く →「Sign up」→「Continue with GitHub」
- [ ] プロジェクトを作る
  - Project name：`habimon-kintore`
  - Region：**Asia Pacific (Singapore)**（日本に一番近い）
- [ ] 「Connect」を押して、接続文字列を出す
- [ ] 接続文字列をコピーして、メモ帳に控える

接続文字列は、こんな形をしている。

```
postgresql://neondb_owner:（パスワード）@ep-xxxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
```

**注意**：この文字にはデータベースのパスワードが入っている。チャット・SNS・GitHub には貼らない。貼るのは Render の画面だけ。

---

## 3. 合言葉を2つ決める

| 名前 | 何に使うか | 決め方 |
|---|---|---|
| `APP_PASSWORD` | スマホで筋トレアプリを開くときの合言葉 | 自分で決める。覚えやすくて、人に当てられにくいもの（12文字以上がおすすめ） |
| `SYNC_TOKEN` | ハビットモンスターが記録を取りに来るときの合言葉 | 覚えなくていい。長いでたらめな文字にする |

`SYNC_TOKEN` は、ハビットモンスターのフォルダで次を実行すると作れる。

```
.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
```

- [ ] `APP_PASSWORD` を決めた
- [ ] `SYNC_TOKEN` を作って、メモ帳に控えた

この2つも、チャットや GitHub には貼らない。

---

## 4. Render：アプリを動かす

- [ ] https://render.com を開く →「Get Started」→「GitHub」で登録
  - クレジットカードは、無料プランだけなら入れなくていい
- [ ] 「New」→「Web Service」を押す
- [ ] GitHub の `habimon-kintore` を選ぶ（出てこないときは「Configure account」で Render に見せるリポジトリを足す）
- [ ] 次のとおりに入力する

| 項目 | 入れる内容 |
|---|---|
| Name | `habimon-kintore`（これがアドレスの一部になる） |
| Region | **Singapore** |
| Branch | `main` |
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Instance Type | **Free** |

- [ ] 「Environment Variables」に、次の6つを入れる

| Key | Value |
|---|---|
| `DATABASE_URL` | 手順2で控えた Neon の接続文字列 |
| `APP_PASSWORD` | 手順3で決めた合言葉 |
| `SYNC_TOKEN` | 手順3で作った文字 |
| `SECRET_KEY` | 「Generate」を押して作る（なければ、`SYNC_TOKEN` と同じやり方で別の文字を作る） |
| `APP_TIMEZONE` | `Asia/Tokyo` |
| `PYTHON_VERSION` | `3.13.5` |

- [ ] 「Advanced」の Health Check Path に `/healthz` を入れる
- [ ] 「Create Web Service」を押す
- [ ] 数分待って「Live」と出たら、上に出ているアドレスを控える

```
控え：https://＿＿＿＿＿＿＿＿＿＿.onrender.com
```

- [ ] そのアドレスをパソコンのブラウザで開いて、合言葉でログインできるか確かめる

**絶対に入れないもの**：`KINTORE_DEV`。これを入れると、合言葉なしでだれでも使えてしまう（パソコンの中で試すとき専用）。

---

## 5. ハビットモンスターとつなぐ

ハビットモンスターのフォルダの `.env` をメモ帳で開いて、最後に2行足す。

```
WORKOUT_APP_URL=https://（手順4で控えたアドレス）
WORKOUT_APP_TOKEN=（手順3の SYNC_TOKEN と同じ文字）
```

- [ ] `.env` に2行足して保存した
- [ ] ハビットモンスターを起動し直した（黒い画面を閉じて、デスクトップのショートカットから開く）
- [ ] 筋トレ画面が「筋トレの記録は 筋トレアプリで」に変わった

---

## 6. 今までの記録を引っ越す（1回だけ）

ハビットモンスターを起動したまま、別の黒い画面（コマンドプロンプト）で次を実行する。

```
curl -X POST http://localhost:8000/api/workouts/push-history
```

`{"pushed":8}` のように、送った件数が出れば成功。もう一度実行しても、同じ記録は増えない。

- [ ] 引っ越しをした
- [ ] 筋トレアプリの「筋トレの履歴」に、今までの記録が出ている

---

## 7. スマホのホーム画面に追加する（iPhone）

- [ ] iPhone の **Safari** で、手順4のアドレスを開く
- [ ] 合言葉を入れてログインする
- [ ] 下の真ん中の「共有」ボタン（□に↑）を押す
- [ ] 「ホーム画面に追加」を押す
- [ ] ホーム画面にダンベルのアイコンができた

次からは、このアイコンから開く。ログインは 180日そのまま使える。

---

## 8. 食事の記録を使えるようにする（AI のキーを入れる）

食事の写真から栄養を見積もるには、OpenAI のキーを Render に入れる。

- [ ] https://platform.openai.com/settings/organization/limits を開いて、**月の上限金額**を決める（例：5ドル）。万一のときも、それ以上はかからない
- [ ] Render の `habimon-kintore` →「Environment」→「Add Environment Variable」
  - Key：`OPENAI_API_KEY`
  - Value：ハビットモンスターの `.env` の `OPENAI_API_KEY=` の右側（`sk-` で始まる文字。前後の `"` は付けない）
- [ ] 「Save Changes」を押す（アプリが自動で入れ直される。1〜2分）
- [ ] スマホで「食事」タブを開いて、「AIに見積もってもらう」が押せるようになっていれば成功

目安：写真1枚で 0.3〜1円くらい。1日に AI を使えるのは 30回まで（アプリ側で止める）。

---

## 使い方（毎回）

1. ジムで、スマホの筋トレアプリを開く（しばらく使っていないと、最初だけ 30秒〜1分かかる）
2. 「筋トレ開始！」→ セットを記録 →「筋トレを終わる」
3. 家でハビットモンスターを開いて、筋トレ画面を見る → 自動で取りこまれて、経験値などがもらえる

---

## うまくいかないとき

| こんなとき | 確かめること |
|---|---|
| Render で「Deploy failed」と出る | Build Command と Start Command が表のとおりか。`PYTHON_VERSION` が `3.13.5` か |
| 開くと「合言葉がまだ設定されていません」と出る | Render の `APP_PASSWORD` と `SECRET_KEY` が入っているか |
| ログインできない | `APP_PASSWORD` の前後に空白が入っていないか。10回まちがえると 10分待つ |
| 時刻が9時間ずれる | `APP_TIMEZONE` が `Asia/Tokyo` か |
| ハビモンで「合言葉がちがうみたい」と出る | `.env` の `WORKOUT_APP_TOKEN` と、Render の `SYNC_TOKEN` が同じ文字か |
| ハビモンで「つながらなかった」と出る | 筋トレアプリが休んでいるだけのことが多い。1分待って「取りこむ」をもう一度押す |
| 食事で「AIがまだ設定されていません」と出る | Render の `OPENAI_API_KEY` が入っているか（手順8） |
| 食事で「AIにつながらなかった」と出る | OpenAI の残高や上限を確かめる。少し待ってもう一度 |
| 記録が消えた気がする | Neon の画面でプロジェクトが止まっていないか（無料プランは、長く使わないと止まることがある） |

---

## 控え（ここには書かない）

合言葉や接続文字列は、このファイルには書かない。このファイルは GitHub に上がるため。
パスワード管理アプリや、紙のメモに控えておく。

| 控えるもの | どこにあるか |
|---|---|
| Neon の接続文字列 | Neon の「Connect」でいつでも見られる |
| `APP_PASSWORD` | Render の Environment で見られる |
| `SYNC_TOKEN` | Render の Environment と、ハビモンの `.env` |
