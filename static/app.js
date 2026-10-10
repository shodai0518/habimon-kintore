// ハビモン筋トレ：筋トレの記録だけをする画面
const view = document.getElementById("view");
const logoutBtn = document.getElementById("logout");
const messageLayer = document.getElementById("message");
const messageText = document.getElementById("message-text");

let part = "chest"; // 選んでいる部位
let exerciseId = null; // 選んでいる種目
let timer = null;

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pad = (n) => String(n).padStart(2, "0");
const localDateTime = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const hhmm = (iso) => {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const WEEKDAYS = "日月火水木金土";
const dayLabel = (iso) => {
  const d = new Date(`${iso}T00:00:00`);
  return `${d.getMonth() + 1}/${d.getDate()}（${WEEKDAYS[d.getDay()]}）`;
};

// ---- セットの間の休憩タイマー ----
// 残り時間は「終わる時刻」から計算するので、別のアプリから戻ってきても 正しい時間が出る
const restBar = document.getElementById("rest");
const restTime = document.getElementById("rest-time");
const restName = document.getElementById("rest-name");
const restSound = document.getElementById("rest-sound");
const REST_KEY = "kintore_rest";
const SOUND_KEY = "kintore_rest_sound";
let rest = null; // { exerciseId, name, seconds（決めた長さ）, endAt（終わる時刻）, rung（もう知らせたか）, limits }
let restTicker = null;
let audio = null;
let wakeLock = null;

const store = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch {
      // 保存できなくても続ける
    }
  },
};
const soundOn = () => store.get(SOUND_KEY) !== "off";

// 音は、ボタンを押したとき（セットの「追加」など）に 用意しておかないと 鳴らせない
function prepareAudio() {
  try {
    audio ??= new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state === "suspended") audio.resume();
  } catch {
    audio = null;
  }
}

function beep() {
  if (!soundOn() || !audio) return;
  [0, 0.25, 0.5].forEach((delay) => {
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.type = "square";
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, audio.currentTime + delay);
    gain.gain.exponentialRampToValueAtTime(0.25, audio.currentTime + delay + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + delay + 0.18);
    osc.connect(gain).connect(audio.destination);
    osc.start(audio.currentTime + delay);
    osc.stop(audio.currentTime + delay + 0.2);
  });
}

// タイマーが動いている間は 画面が暗くならないようにする（暗くなると 音が鳴らせない）
async function keepAwake(on) {
  try {
    if (on && !wakeLock && "wakeLock" in navigator && document.visibilityState === "visible") {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => (wakeLock = null));
    } else if (!on && wakeLock) {
      await wakeLock.release();
      wakeLock = null;
    }
  } catch {
    wakeLock = null;
  }
}

const formatRest = (seconds) => `${Math.floor(seconds / 60)}:${pad(seconds % 60)}`;

function drawRest() {
  if (!rest) return;
  const left = Math.ceil((rest.endAt - Date.now()) / 1000);
  const over = left <= 0;
  restTime.textContent = over ? `+${formatRest(-left)}` : formatRest(left);
  restBar.classList.toggle("over", over);
  if (over && !rest.rung) {
    rest.rung = true;
    saveRest();
    beep();
    restBar.classList.add("flash");
    setTimeout(() => restBar.classList.remove("flash"), 2400);
  }
  if (-left > 600) stopRest(); // 10分すぎても そのままなら 片づける
}

function saveRest() {
  store.set(REST_KEY, rest ? JSON.stringify(rest) : null);
}

function showRest() {
  restBar.hidden = false;
  document.body.classList.add("resting");
  restName.textContent = `（${rest.name}・${formatRest(rest.seconds)}）`;
  restSound.textContent = soundOn() ? "音 オン" : "音 オフ";
  clearInterval(restTicker);
  restTicker = setInterval(drawRest, 250);
  drawRest();
  keepAwake(true);
}

function startRest(exercise, limits) {
  if (!exercise?.rest_seconds) return;
  prepareAudio();
  rest = {
    exerciseId: exercise.id,
    name: exercise.name,
    seconds: exercise.rest_seconds,
    endAt: Date.now() + exercise.rest_seconds * 1000,
    rung: false,
    limits,
  };
  saveRest();
  showRest();
}

function stopRest() {
  rest = null;
  saveRest();
  clearInterval(restTicker);
  restBar.hidden = true;
  restBar.classList.remove("over", "flash");
  document.body.classList.remove("resting");
  keepAwake(false);
}

// 長さを変える：今のタイマーに足し引きして、この種目の長さとして覚える
function adjustRest(delta) {
  if (!rest) return;
  prepareAudio();
  const { min = 15, max = 600 } = rest.limits ?? {};
  const seconds = Math.min(max, Math.max(min, rest.seconds + delta));
  const change = seconds - rest.seconds;
  if (!change) return;
  rest.seconds = seconds;
  rest.endAt += change * 1000;
  if (rest.endAt > Date.now()) rest.rung = false; // のばして まだ時間が残っていれば、また知らせる
  saveRest();
  showRest();
  api(`/exercises/${rest.exerciseId}/rest`, { method: "PUT", body: { seconds } }).catch(() => {});
}

document.getElementById("rest-minus").addEventListener("click", () => adjustRest(-(rest?.limits?.step ?? 15)));
document.getElementById("rest-plus").addEventListener("click", () => adjustRest(rest?.limits?.step ?? 15));
document.getElementById("rest-skip").addEventListener("click", stopRest);
restSound.addEventListener("click", () => {
  store.set(SOUND_KEY, soundOn() ? "off" : "on");
  restSound.textContent = soundOn() ? "音 オン" : "音 オフ";
  if (soundOn()) {
    prepareAudio();
    beep(); // どんな音か 聞かせる
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && rest) {
    keepAwake(true);
    drawRest();
  }
});

// 画面を開き直したときに、動いていたタイマーを つづける
function restoreRest() {
  try {
    const saved = JSON.parse(store.get(REST_KEY) || "null");
    if (saved && Date.now() - saved.endAt < 600000) {
      rest = saved;
      showRest();
    } else {
      store.set(REST_KEY, null);
    }
  } catch {
    store.set(REST_KEY, null);
  }
}

class LoginNeeded extends Error {}

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    method: options.method ?? "GET",
    headers: options.body ? { "Content-Type": "application/json" } : {},
    body: options.body ? JSON.stringify(options.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && path !== "/login") throw new LoginNeeded();
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? "入力した内容を確かめてね" : data.detail;
    throw new Error(detail || "うまくいかなかった…もう一度試してね");
  }
  return data;
}

// メッセージを1つずつ出す（タップで次へ）
function showMessages(lines) {
  if (!lines?.length) return Promise.resolve();
  return new Promise((resolve) => {
    let index = 0;
    const show = () => (messageText.textContent = lines[index]);
    const next = () => {
      index += 1;
      if (index < lines.length) return show();
      messageLayer.hidden = true;
      messageLayer.removeEventListener("click", next);
      resolve();
    };
    messageLayer.hidden = false;
    messageLayer.addEventListener("click", next);
    show();
  });
}

async function run(action) {
  try {
    await action();
  } catch (err) {
    if (err instanceof LoginNeeded) return renderLogin();
    await showMessages([err.message]);
  }
}

// 2回押して たしかめるボタン
function confirmTwice(button, warning, action) {
  const label = button.textContent;
  button.addEventListener("click", () => {
    if (button.dataset.armed) return action();
    button.dataset.armed = "1";
    button.textContent = warning;
    setTimeout(() => {
      delete button.dataset.armed;
      button.textContent = label;
    }, 4000);
  });
}

// ---- ログイン ----
function renderLogin() {
  clearInterval(timer);
  stopRest();
  logoutBtn.hidden = true;
  view.innerHTML = `
    <section class="window">
      <h2>ログイン</h2>
      <form id="login-form">
        <input type="password" name="password" autocomplete="current-password" placeholder="合言葉" required style="width:100%">
        <button class="primary" style="margin-top:10px">入る</button>
      </form>
      <p class="hint">一度入ると、このスマホでは しばらく入力しなくて大丈夫だよ。</p>
    </section>`;
  document.getElementById("login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    run(async () => {
      await api("/login", { method: "POST", body: { password: new FormData(e.target).get("password") } });
      await render();
    });
  });
}

logoutBtn.addEventListener("click", () =>
  run(async () => {
    await api("/logout", { method: "POST" });
    renderLogin();
  }),
);

// ---- 始める前の画面 ----
function renderStart(data) {
  const now = new Date();
  const minDay = new Date(now.getTime() - data.backfill_days * 86400000);
  const weight = data.last_weight ?? "";
  const fat = data.last_fat ?? "";
  const bodyInputs = (prefix) => `
    <div class="row" style="margin-top:6px"><span style="width:3em">体重</span>
      <input type="number" name="weight" id="${prefix}-weight" step="0.1" min="0" value="${weight}" inputmode="decimal" style="width:6em"> kg
      <span>体脂肪</span><input type="number" name="fat" step="0.1" min="0" max="80" value="${fat}" inputmode="decimal" style="width:4.5em" placeholder="任意"> %</div>`;
  view.innerHTML = `
    <section class="window">
      <h2>今から筋トレ</h2>
      <form id="start-form">
        ${bodyInputs("start")}
        <button class="primary" style="margin-top:10px">筋トレ開始！</button>
      </form>
      <p class="hint">体重は筋トレの前に測ったものを。体脂肪率は大まかな値でOK（なくてもOK）。</p>
    </section>
    <section class="window">
      <h2>あとから記録する</h2>
      <form id="backfill-form">
        <div class="row"><span style="width:3em">開始</span><input type="datetime-local" name="start" value="${localDateTime(new Date(now.getTime() - 3600000))}" min="${localDateTime(minDay)}" max="${localDateTime(now)}"></div>
        <div class="row" style="margin-top:6px"><span style="width:3em">終了</span><input type="datetime-local" name="end" value="${localDateTime(now)}" min="${localDateTime(minDay)}" max="${localDateTime(now)}"></div>
        ${bodyInputs("backfill")}
        <button style="margin-top:10px">この時間で記録を作る</button>
      </form>
      <p class="hint">${data.backfill_days}日前の分まで記録できるよ。作ったあとに、種目とセットを入れてね。</p>
    </section>
    ${historySection(data.history)}`;

  const body = (form) => {
    const f = new FormData(form);
    return { body_weight: f.get("weight") ? Number(f.get("weight")) : null, body_fat: f.get("fat") ? Number(f.get("fat")) : null };
  };
  document.getElementById("start-form").addEventListener("submit", (e) => {
    e.preventDefault();
    run(async () => {
      await api("/workouts/start", { method: "POST", body: body(e.target) });
      await render();
    });
  });
  document.getElementById("backfill-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    if (!f.get("start") || !f.get("end")) return;
    if (f.get("end") <= f.get("start")) return showMessages(["終了時刻は、開始時刻よりあとにしてね。"]);
    run(async () => {
      await api("/workouts/start", { method: "POST", body: { ...body(e.target), started_at: f.get("start"), planned_end: f.get("end") } });
      await render();
    });
  });
}

function historySection(history) {
  const items = history.length
    ? history
        .map(
          (w) => `
        <li>
          <b>${dayLabel(w.day)}</b> <small class="hint">${hhmm(w.started_at)}〜${hhmm(w.ended_at)}・${w.minutes}分${w.body_weight ? `・${w.body_weight}kg` : ""}</small>
          ${w.exercises
            .map(
              (g) => `<div class="history-line"><span>${esc(g.name)}${g.kcal ? `<br><small class="kcal">約${g.kcal}kcal${g.warmup ? `（アップ${g.warmup}セット込み）` : ""}</small>` : ""}</span><span>${g.sets
                .map((s) => (s.is_pr ? `<span class="ok">★${esc(s.label)}</span>` : esc(s.label)))
                .join("、")}</span></div>`,
            )
            .join("")}
          ${w.kcal !== null ? `<div class="kcal">消費カロリー 約${w.kcal}kcal</div>` : ""}
          ${w.memo ? `<small class="hint">${esc(w.memo)}</small>` : ""}
        </li>`,
        )
        .join("")
    : '<li class="empty">まだ記録がありません</li>';
  return `<section class="window"><h2>筋トレの履歴（最近90日）</h2><ul class="list">${items}</ul>
    <p class="hint">グラフや経験値は、パソコンのハビットモンスターで見られるよ。</p></section>`;
}

// ---- 記録中の画面 ----
function renderActive(w, ex) {
  const exercises = ex.exercises.filter((e) => e.part === part);
  if (!exercises.some((e) => e.id === exerciseId)) exerciseId = exercises[0]?.id ?? null;
  const selected = ex.exercises.find((e) => e.id === exerciseId);
  // 入力の初期値：今日の最後のセット → 前回の最後のセット
  const todayGroup = w.exercises.find((g) => g.exercise_id === exerciseId);
  const lastSet = todayGroup ? todayGroup.sets[todayGroup.sets.length - 1] : selected?.last;
  const plannedEnd = w.planned_end ? w.planned_end.slice(0, 16) : null;

  const setInputs = selected?.cardio
    ? `<input type="number" name="duration" step="1" min="1" placeholder="時間" inputmode="numeric" style="width:5em" value="${lastSet?.duration_min ?? ""}"> 分
       <input type="number" name="distance" step="0.1" min="0" placeholder="距離" inputmode="decimal" style="width:5em" value="${lastSet?.distance_km ?? ""}"> km`
    : `<input type="number" name="weight" step="0.5" min="0" placeholder="重さ" inputmode="decimal" style="width:5em" value="${lastSet?.weight ?? ""}"> kg ×
       <input type="number" name="reps" step="1" min="1" placeholder="回数" inputmode="numeric" style="width:4em" value="${lastSet?.reps ?? ""}"> 回`;

  const groups = w.exercises.length
    ? w.exercises
        .map(
          (g) => `
          <li>
            <b>${esc(g.name)}</b> <small class="hint">${g.part_label}${g.warmup ? `・アップ${g.warmup}セット` : ""}${g.kcal ? `・約${g.kcal}kcal` : ""}</small>
            <div class="set-chips">${g.sets
              .map((s, i) => `<span class="set-chip">${i + 1}. ${esc(s.label)}<button class="small" data-set-delete="${s.id}" aria-label="消す">×</button></span>`)
              .join("")}</div>
          </li>`,
        )
        .join("")
    : '<li class="empty">まだセットがありません</li>';

  view.innerHTML = `
    <section class="window center">
      <h2>筋トレ中</h2>
      <p class="clock" id="clock">--:--</p>
      <p class="hint">開始 ${hhmm(w.started_at)}${plannedEnd ? `（あとから記録：終了 ${hhmm(plannedEnd)}）` : ""}</p>
    </section>
    <section class="window">
      <h2>セットを追加</h2>
      <div class="row part-row">
        ${Object.entries(ex.parts).map(([key, label]) => `<button type="button" class="small ${key === part ? "selected" : ""}" data-part="${key}">${label}</button>`).join("")}
      </div>
      <form id="set-form" style="margin-top:8px">
        <div class="row">
          <select name="exercise" class="grow">
            ${exercises.map((e) => `<option value="${e.id}" ${e.id === exerciseId ? "selected" : ""}>${esc(e.name)}</option>`).join("")}
          </select>
          <button type="button" class="small" id="new-exercise">＋種目</button>
        </div>
        ${
          selected
            ? `<p class="hint">前回：${selected.last ? `${dayLabel(selected.last.day)} ${selected.last.sets.map(esc).join("、")}` : "まだ記録なし"}${
                selected.best ? ` ／ 自己ベスト：${esc(selected.best)}` : ""
              }</p>`
            : ""
        }
        <div class="row" style="margin-top:6px">${setInputs}<button class="grow">追加</button></div>
        ${
          selected && !selected.cardio
            ? `<div class="row" style="margin-top:6px">
                 <span class="hint" style="margin:0">本番の前のアップ</span>
                 <select id="warmup-sets">${[0, 1, 2, 3, 4, 5]
                   .map((n) => `<option value="${n}" ${n === (w.warmups[selected.id] ?? selected.warmup_sets) ? "selected" : ""}>${n === 0 ? "なし" : `${n}セット`}</option>`)
                   .join("")}</select>
               </div>`
            : ""
        }
      </form>
      <form id="exercise-form" class="row" style="margin-top:8px" hidden>
        <input name="name" class="grow" maxlength="30" placeholder="新しい種目の名前">
        <button class="small">登録</button>
      </form>
    </section>
    <section class="window">
      <h2>今日のセット（${w.set_count}）</h2>
      <p class="hint" style="margin-top:0">${
        w.kcal !== null ? `消費カロリー（今の時点の目安）：約${w.kcal}kcal（体重 ${w.kcal_weight}kg で計算）` : "体重を入れると、消費カロリーの目安が出るよ"
      }</p>
      <ul class="list">${groups}</ul>
    </section>
    <section class="window">
      <form id="body-form" class="row">
        <span>体重</span><input type="number" name="weight" step="0.1" min="0" value="${w.body_weight ?? ""}" inputmode="decimal" style="width:5.5em"> kg
        <span>体脂肪</span><input type="number" name="fat" step="0.1" min="0" max="80" value="${w.body_fat ?? ""}" inputmode="decimal" style="width:4.5em"> %
        <input name="memo" class="grow" maxlength="200" placeholder="メモ（調子など）" value="${esc(w.memo ?? "")}" style="flex-basis:100%">
        <button class="small">保存</button>
      </form>
    </section>
    <section class="window">
      <form id="finish-form">
        <div class="row"><span>終了</span><input type="datetime-local" name="end" value="${plannedEnd ?? localDateTime(new Date())}"></div>
        <div class="row" style="margin-top:10px">
          <button type="button" id="finish" class="grow">筋トレを終わる</button>
          <button type="button" id="cancel" class="small danger">やめる</button>
        </div>
      </form>
      <p class="hint">「やめる」を押すと、この筋トレの記録は消えるよ。</p>
    </section>`;

  // 経過時間
  const clock = document.getElementById("clock");
  const started = new Date(w.started_at).getTime();
  const tick = () => {
    const end = plannedEnd ? new Date(plannedEnd).getTime() : Date.now();
    const minutes = Math.max(0, Math.floor((end - started) / 60000));
    clock.textContent = `${Math.floor(minutes / 60)}:${pad(minutes % 60)}`;
  };
  tick();
  clearInterval(timer);
  if (!plannedEnd) timer = setInterval(tick, 15000);

  view.querySelectorAll("[data-part]").forEach((btn) =>
    btn.addEventListener("click", () => {
      part = btn.dataset.part;
      exerciseId = null;
      renderActive(w, ex);
    }),
  );
  view.querySelector('select[name="exercise"]').addEventListener("change", (e) => {
    exerciseId = Number(e.target.value);
    renderActive(w, ex);
  });
  document.getElementById("new-exercise").addEventListener("click", () => {
    const form = document.getElementById("exercise-form");
    form.hidden = !form.hidden;
    if (!form.hidden) form.querySelector("input").focus();
  });
  document.getElementById("exercise-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = new FormData(e.target).get("name").trim();
    if (!name) return;
    run(async () => {
      const created = await api("/exercises", { method: "POST", body: { name, part } });
      exerciseId = created.id;
      await render();
    });
  });

  const warmup = document.getElementById("warmup-sets");
  document.getElementById("set-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!exerciseId) return;
    const f = new FormData(e.target);
    const num = (key) => (f.get(key) ? Number(f.get(key)) : null);
    const body = selected?.cardio
      ? { exercise_id: exerciseId, duration_min: num("duration"), distance_km: num("distance") }
      : { exercise_id: exerciseId, weight: num("weight"), reps: num("reps") };
    run(async () => {
      // アップの数を まだ伝えていなければ、先に伝える
      if (warmup && w.warmups[exerciseId] === undefined) {
        await api(`/workouts/${w.id}/warmup`, { method: "PUT", body: { exercise_id: exerciseId, sets: Number(warmup.value) } });
      }
      prepareAudio(); // 「追加」を押した今のうちに 音を用意しておく
      await api(`/workouts/${w.id}/sets`, { method: "POST", body });
      if (selected && !selected.cardio) startRest(selected, ex.rest);
      await render();
    });
  });
  warmup?.addEventListener("change", () =>
    run(async () => {
      await api(`/workouts/${w.id}/warmup`, { method: "PUT", body: { exercise_id: exerciseId, sets: Number(warmup.value) } });
      await render();
    }),
  );
  view.querySelectorAll("[data-set-delete]").forEach((btn) =>
    btn.addEventListener("click", () =>
      run(async () => {
        await api(`/workouts/sets/${btn.dataset.setDelete}`, { method: "DELETE" });
        await render();
      }),
    ),
  );
  document.getElementById("body-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    run(async () => {
      await api(`/workouts/${w.id}`, {
        method: "PATCH",
        body: { body_weight: f.get("weight") ? Number(f.get("weight")) : null, body_fat: f.get("fat") ? Number(f.get("fat")) : null, memo: f.get("memo") || null },
      });
      await render();
    });
  });
  document.getElementById("finish-form").addEventListener("submit", (e) => e.preventDefault());
  confirmTwice(document.getElementById("finish"), "本当に終わる？（もう一度押す）", () => {
    const end = new FormData(document.getElementById("finish-form")).get("end");
    run(async () => {
      const res = await api(`/workouts/${w.id}/finish`, { method: "POST", body: { ended_at: end || null } });
      stopRest();
      await showMessages(res.messages);
      await render();
    });
  });
  confirmTwice(document.getElementById("cancel"), "本当にやめる？", () =>
    run(async () => {
      await api(`/workouts/${w.id}`, { method: "DELETE" });
      stopRest();
      await render();
    }),
  );
}

async function render() {
  clearInterval(timer);
  await run(async () => {
    const data = await api("/workouts");
    logoutBtn.hidden = false;
    if (!data.current) {
      if (rest) stopRest(); // 筋トレが終わっていたら タイマーも片づける
      return renderStart(data);
    }
    renderActive(data.current, await api("/exercises"));
  });
}

restoreRest();
render();
