// ハビモン筋トレ：筋トレの記録だけをする画面
const view = document.getElementById("view");
const logoutBtn = document.getElementById("logout");
const messageLayer = document.getElementById("message");
const messageText = document.getElementById("message-text");

let part = "chest"; // 選んでいる部位
let exerciseId = null; // 選んでいる種目
let exerciseChanged = false; // 種目を 自分で変えた直後か（休憩タイマーを片づけるかの判断に使う）
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

// ---- ロック画面への通知（画面を閉じていても、休憩が終わったら知らせる）----
// スマホの画面を閉じると このアプリは止まるので、サーバーに「この時刻に知らせて」と頼んでおく
const push = { available: false, publicKey: null, on: false, loaded: false };
const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

async function currentSubscription() {
  const reg = await navigator.serviceWorker.getRegistration("/");
  return reg ? reg.pushManager.getSubscription() : null;
}

// 公開鍵（base64url）を、登録に使う形にする
function keyBytes(text) {
  const base = (text + "=".repeat((4 - (text.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(base), (c) => c.charCodeAt(0));
}

async function loadPush() {
  if (push.loaded) return;
  push.loaded = true;
  try {
    const info = await api("/push");
    push.available = info.available;
    push.publicKey = info.public_key;
    if (push.available && pushSupported() && Notification.permission === "granted") {
      const sub = await currentSubscription();
      push.on = !!sub;
      if (sub) api("/push/subscribe", { method: "POST", body: sub.toJSON() }).catch(() => {}); // サーバーが忘れていても 思い出せるように
    }
  } catch {
    push.loaded = false;
  }
  drawPushLine();
}

async function turnPushOn() {
  if (!pushSupported()) throw new Error("この開き方では 通知が使えないよ。ホーム画面に追加したアプリから開いてね。");
  if ((await Notification.requestPermission()) !== "granted") {
    throw new Error("通知が許可されていないよ。iPhoneの「設定 → 通知 → 筋トレ」で 許可してね。");
  }
  const reg = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;
  const sub = (await reg.pushManager.getSubscription()) ?? (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(push.publicKey) }));
  await api("/push/subscribe", { method: "POST", body: sub.toJSON() });
  push.on = true;
}

async function turnPushOff() {
  const sub = await currentSubscription();
  if (sub) {
    await api("/push/unsubscribe", { method: "POST", body: { endpoint: sub.endpoint } });
    await sub.unsubscribe();
  }
  push.on = false;
}

function pushLineHtml() {
  if (!push.available) return "";
  if (!pushSupported()) return "休憩おわりの通知：ホーム画面に追加したアプリから開くと、ロック画面に通知を出せるよ。";
  return push.on
    ? `休憩おわりの通知：<b class="ok">オン</b>　<button type="button" class="small" data-push="test">テスト</button> <button type="button" class="small" data-push="off">オフにする</button>`
    : `休憩おわりの通知：オフ　<button type="button" class="small" data-push="on">ロック画面に通知する</button>`;
}

// 始める前の画面の「通知」の行を 描く（まだ読みこみ中なら 空のまま）
function drawPushLine() {
  const line = document.getElementById("push-line");
  if (!line) return;
  line.innerHTML = pushLineHtml();
  line.hidden = !line.innerHTML;
  line.querySelectorAll("[data-push]").forEach((btn) =>
    btn.addEventListener("click", () =>
      run(async () => {
        btn.disabled = true;
        try {
          if (btn.dataset.push === "on") {
            await turnPushOn();
            await showMessages(["通知をオンにした！", "休憩が終わったら、画面を閉じていても 通知が届くよ。"]);
          } else if (btn.dataset.push === "off") {
            await turnPushOff();
          } else {
            const res = await api("/push/test", { method: "POST" });
            await showMessages([`${res.seconds}秒後に 通知を送るよ。`, "今のうちに 画面をロックして 待ってみてね。"]);
          }
        } finally {
          btn.disabled = false;
          drawPushLine();
        }
      }),
    ),
  );
}

// 休憩が終わる時刻に 通知を出してもらうように 頼む（長さを変えたら 頼み直す）
function bookRestNotice() {
  if (!push.on || !rest) return;
  const seconds = (rest.endAt - Date.now()) / 1000;
  if (seconds <= 0) return cancelRestNotice();
  api("/rest/notify", { method: "POST", body: { seconds, name: rest.name } }).catch(() => {});
}

function cancelRestNotice() {
  if (!push.on) return;
  api("/rest/notify", { method: "DELETE" }).catch(() => {});
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
    if (document.visibilityState === "visible") cancelRestNotice(); // 画面を見ているので、ロック画面への通知は いらない
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
  bookRestNotice();
}

function stopRest() {
  if (rest) cancelRestNotice();
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
  bookRestNotice();
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

// ---- 相棒（ハビットモンスターから届く。見るだけ）----
const partnerBtn = document.getElementById("partner");
const appName = document.getElementById("app-name");
let partner = null;
let partnerLoadedAt = 0;

function showPartner() {
  partnerBtn.hidden = !partner;
  appName.hidden = !!partner;
  document.querySelector(".status-window").classList.toggle("with-partner", !!partner);
  if (!partner) return;
  partnerBtn.innerHTML = `
    ${partner.image_url ? `<img class="${partner.motion === "egg" ? "egg" : ""}" src="${partner.image_url}" alt="">` : ""}
    <span class="partner-text"><span class="name">${esc(partner.name)}</span><small>ランク${esc(partner.rank)} Lv${partner.level}</small></span>`;
}

// 画面を描くついでに 取りにいく（1分に1回まで。失敗しても 記録のじゃまをしない）
async function loadPartner() {
  if (Date.now() - partnerLoadedAt < 60000) return;
  partnerLoadedAt = Date.now();
  try {
    partner = (await api("/partner")).partner;
    showPartner();
  } catch {
    partnerLoadedAt = 0;
  }
}

function hidePartner() {
  partner = null;
  partnerLoadedAt = 0;
  showPartner();
}

// 大きく見る。絵を押すと なでられる（動いて ハートが出るだけ。機嫌は ハビットモンスターで なでたときに上がる）
partnerBtn.addEventListener("click", () => {
  if (!partner?.image_url) return;
  const back = document.createElement("div");
  back.className = "zoom-back";
  back.innerHTML = `<div class="window zoom-box">
    <button type="button" class="small zoom-close" aria-label="閉じる">×</button>
    <div class="pet-field"><img class="pet-target ${partner.motion === "egg" ? "egg" : ""}" src="${partner.image_url}" alt="${esc(partner.name)}をなでる"></div>
    <p><span class="name">${esc(partner.name)}</span>　ランク${esc(partner.rank)} Lv${partner.level}</p>
    <p class="hint">タップで なでられるよ</p>
  </div>`;
  back.addEventListener("click", (e) => {
    if (e.target === back || e.target.closest(".zoom-close")) back.remove();
  });
  const img = back.querySelector(".pet-target");
  const field = back.querySelector(".pet-field");
  img.addEventListener("click", () => {
    const cls = `pet-${partner.motion}`;
    img.classList.remove(cls);
    void img.offsetWidth; // 動きを 最初から やり直す
    img.classList.add(cls);
    for (let i = 0; i < 3; i++) {
      const heart = document.createElement("span");
      heart.className = "pet-heart";
      heart.textContent = "♥";
      heart.style.left = `${38 + i * 10 + Math.random() * 8}%`;
      heart.style.animationDelay = `${i * 0.15}s`;
      field.appendChild(heart);
      setTimeout(() => heart.remove(), 1600);
    }
  });
  // 動きが終わったら、いつもの動きに戻す
  img.addEventListener("animationend", (e) => e.animationName.startsWith("pet-") && img.classList.remove(`pet-${partner.motion}`));
  document.body.appendChild(back);
});

// ---- 食事の記録 ----
const TAB_KEY = "kintore_tab";
const tabsEl = document.getElementById("tabs");
let tab = "workout"; // workout（筋トレ） / meal（食事）
let mealPhotos = []; // 選んだ写真（小さくした data URL）
let mealNote = "";
let mealDraft = null; // AIの見積もり（まだ記録していない）{ items, questions, comment, source }

tabsEl.querySelectorAll("[data-tab]").forEach((btn) =>
  btn.addEventListener("click", () => {
    tab = btn.dataset.tab;
    store.set(TAB_KEY, tab);
    render();
  }),
);

// 写真を 長いほうの辺が 1024px になるように小さくして、JPEG にする（送る量と AIの費用をへらす）
async function shrinkPhoto(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error("写真を読みこめなかった。別の写真で試してね"));
      el.src = url;
    });
    const scale = Math.min(1, 1024 / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.8);
  } finally {
    URL.revokeObjectURL(url);
  }
}

// 写真を 左に90度回す（真上から撮ると、スマホが向きを まちがえることがあるので）
async function rotatePhoto(dataUrl) {
  const img = await new Promise((resolve, reject) => {
    const el = new Image();
    el.onload = () => resolve(el);
    el.onerror = () => reject(new Error("写真を回せなかった"));
    el.src = dataUrl;
  });
  const canvas = document.createElement("canvas");
  canvas.width = img.naturalHeight;
  canvas.height = img.naturalWidth;
  const ctx = canvas.getContext("2d");
  ctx.translate(0, canvas.height);
  ctx.rotate(-Math.PI / 2);
  ctx.drawImage(img, 0, 0);
  return canvas.toDataURL("image/jpeg", 0.9);
}

const num1 = (v) => Math.round(Number(v) * 10) / 10;
const sumOf = (items, key) => num1(items.reduce((total, i) => total + (Number(i[key]) || 0), 0));

function targetLine(label, value, unit, text, state) {
  return `<li class="meal-total"><span>${label}</span><b>${value}${unit}</b><small class="${state}">${text}</small></li>`;
}

function mealSummary(data) {
  const t = data.totals;
  const g = data.targets;
  const protein = g
    ? t.protein >= g.protein_min
      ? targetLine("たんぱく質", t.protein, "g", `目標 ${g.protein_min}g 達成！`, "ok")
      : targetLine("たんぱく質", t.protein, "g", `目標 ${g.protein_min}g（あと${num1(g.protein_min - t.protein)}g）`, "hint")
    : targetLine("たんぱく質", t.protein, "g", "", "hint");
  const fat = g
    ? t.fat > g.fat_max
      ? targetLine("脂質", t.fat, "g", `目標 ${g.fat_min}〜${g.fat_max}g（${num1(t.fat - g.fat_max)}g オーバー）`, "ng")
      : t.fat >= g.fat_min
        ? targetLine("脂質", t.fat, "g", `目標 ${g.fat_min}〜${g.fat_max}g の中`, "ok")
        : targetLine("脂質", t.fat, "g", `目標 ${g.fat_min}〜${g.fat_max}g（あと${num1(g.fat_min - t.fat)}g で範囲）`, "hint")
    : targetLine("脂質", t.fat, "g", "", "hint");
  const e = data.energy;
  const energy = e.bmr
    ? `<ul class="meal-totals energy">
        <li class="meal-total"><span>基礎代謝</span><b>${e.bmr.value}kcal</b><small class="hint">何もしなくても使う分（目安）</small></li>
        <li class="meal-total"><span>筋トレ</span><b>${e.workout_kcal}kcal</b><small class="hint">今日の筋トレで使った分</small></li>
        <li class="meal-total"><span>使った合計</span><b>${e.burned}kcal</b><small class="hint">食べた分との差 ${e.balance > 0 ? "+" : ""}${e.balance}kcal</small></li>
      </ul>`
    : `<p class="hint">基礎代謝は、筋トレのときに体重と体脂肪率を記録すると出るよ（体脂肪率がないときは、ハビモンのプロフィールを使う）。</p>`;
  return `<section class="window">
    <h2>今日の食事（${dayLabel(data.today)}）</h2>
    <p class="meal-kcal">${t.kcal}<small> kcal</small></p>
    ${energy}
    <ul class="meal-totals">
      ${protein}
      ${fat}
      ${targetLine("炭水化物", t.carbs, "g", "", "hint")}
    </ul>
    <p class="hint">${
      g
        ? `目標は体重 ${g.weight}kg から計算（たんぱく質 ×${g.protein_per_kg ?? 2}g 以上、脂質 ×0.6〜1.0g）。達成すると、次の日にハビモンでご褒美がもらえるよ。`
        : "筋トレのときに体重を記録すると、たんぱく質と脂質の目標が出るよ。"
    }</p>
  </section>`;
}

function mealAddSection(data) {
  const left = data.ai.limit - data.ai.used;
  return `<section class="window">
    <h2>食事を記録する</h2>
    <form id="meal-form">
      <label class="photo-pick ${mealPhotos.length >= data.max_images ? "full" : ""}">
        <input type="file" id="meal-photos" accept="image/*" multiple hidden ${mealPhotos.length >= data.max_images ? "disabled" : ""}>
        <span class="button-like">${
          mealPhotos.length === 0
            ? "写真を撮る・選ぶ"
            : mealPhotos.length >= data.max_images
              ? `写真は ${data.max_images}枚まで`
              : `写真を追加する（今 ${mealPhotos.length}枚／${data.max_images}枚まで）`
        }</span>
      </label>
      ${
        mealPhotos.length
          ? `<div class="photo-row">${mealPhotos
              .map((p, i) => `<span class="photo-thumb"><img src="${p}" alt="写真${i + 1}"><button type="button" data-photo-remove="${i}" aria-label="この写真を消す">×</button><button type="button" class="photo-rotate" data-photo-rotate="${i}" aria-label="この写真を左に回す"><svg style="transform: scaleX(-1)" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 12a8 8 0 1 1-3-6.2"/><path d="M20 3v5h-5"/></svg></button></span>`)
              .join("")}</div>`
          : ""
      }
      <textarea id="meal-note" rows="2" maxlength="300" placeholder="ひとこと（なくてもOK）例：ご飯大盛り／すき家の牛丼並盛／半分残した">${esc(mealNote)}</textarea>
      <div class="row" style="margin-top:8px">
        <button id="meal-analyze" class="grow" ${data.ai.available && left > 0 ? "" : "disabled"}>AIに見積もってもらう</button>
        <button type="button" id="meal-manual" class="small">自分で入力</button>
      </div>
    </form>
    <p class="hint">${
      data.ai.available
        ? `写真は ${data.max_images}枚まで。1枚ずつ撮って足していけるよ。<br>・料理：真上と斜めから撮ると、量が分かりやすい（箸やスプーンも写す）<br>AIは 今日あと${left}回。`
        : "AIがまだ設定されていないので、数字を自分で入れてね。"
    }</p>
  </section>`;
}

// マイメニュー：よく食べるものを 選ぶだけで記録する（AIは使わない）
let mealListOpen = false; // 「今日 食べたもの」を開いているか（記録した直後は 開く。画面を開き直すと たたまれる）
let menuQuery = ""; // マイメニューの検索に入れた文字
let mealAi = { available: false }; // AIが使えるか（品目の計算に使う）
let menuOpen = false; // 「ほかのマイメニュー」を開いているか

// ひらがな・カタカナ、全角・半角、大文字・小文字の ちがいを気にせずに 探せるようにする
function searchKey(text) {
  return String(text)
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[ァ-ヶ]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0x60))
    .replace(/\s+/g, "");
}

function menuItemHtml(m, options) {
  return `<li class="menu-item">
    <div class="menu-head">
      <div class="menu-main">
        <b>${esc(m.name)}</b>
        <small class="hint">${m.kcal}kcal・たんぱく質 ${m.protein}g・脂質 ${m.fat}g・炭水化物 ${m.carbs}g</small>
      </div>
      <button class="small star ${m.favorite ? "on" : ""}" data-menu-favorite="${m.id}" aria-label="${m.favorite ? "お気に入りから外す" : "お気に入りにする"}">${m.favorite ? "★" : "☆"}</button>
    </div>
    <div class="row menu-actions">
      <select data-menu-servings="${m.id}" aria-label="量">${options}</select>
      <button data-menu-eat="${m.id}">食べた</button>
      <button class="small" data-menu-edit="${m.id}">直す</button>
      <button class="small danger" data-menu-delete="${m.id}">消す</button>
    </div>
  </li>`;
}

// マイメニューの一覧（お気に入りは いつも見える。ほかは たためる。検索中は 当てはまるものを ぜんぶ出す）
function menuListHtml(data) {
  const options = data.servings.map((n) => `<option value="${n}" ${n === 1 ? "selected" : ""}>×${n}</option>`).join("");
  const key = searchKey(menuQuery);
  if (key) {
    const hits = data.menu.filter((m) => searchKey(m.name).includes(key));
    return hits.length
      ? `<ul class="list">${hits.map((m) => menuItemHtml(m, options)).join("")}</ul>`
      : '<p class="empty">当てはまるマイメニューがありません</p>';
  }
  const favorites = data.menu.filter((m) => m.favorite);
  const others = data.menu.filter((m) => !m.favorite);
  const favoriteHtml = favorites.length
    ? `<ul class="list">${favorites.map((m) => menuItemHtml(m, options)).join("")}</ul>`
    : '<p class="hint" style="margin-top:0">「ほかのマイメニュー」を開いて ☆を押すと、お気に入りになって いつもここに出るよ。</p>';
  const otherHtml = others.length
    ? `<details class="menu-others" ${menuOpen ? "open" : ""}>
         <summary>ほかのマイメニュー（${others.length}）</summary>
         <ul class="list">${others.map((m) => menuItemHtml(m, options)).join("")}</ul>
       </details>`
    : "";
  return favoriteHtml + otherHtml;
}

// マイメニュー：よく食べるものを 選ぶだけで記録する（AIは使わない）
function menuSection(data) {
  if (!data.menu.length) {
    return `<section class="window">
      <h2>マイメニュー</h2>
      <p class="hint" style="margin-top:0">よく食べるものを登録しておくと、次からは押すだけで記録できるよ（AIを使わないので、ずれない）。<br>
        記録するときに「マイメニューにも登録」にチェックするか、下の「今日 食べたもの」の「登録」から。</p>
    </section>`;
  }
  return `<section class="window">
    <h2>マイメニュー（${data.menu.length}）</h2>
    <input type="search" id="menu-search" placeholder="名前で探す" value="${esc(menuQuery)}" autocomplete="off" style="width:100%">
    <div id="menu-list" style="margin-top:8px">${menuListHtml(data)}</div>
  </section>`;
}

// マイメニューの一覧の ボタンを つなぐ（検索で 一覧だけ描き直したときにも 呼ぶ）
function bindMenuList(data) {
  const list = document.getElementById("menu-list");
  if (!list) return;
  list.querySelector(".menu-others")?.addEventListener("toggle", (e) => (menuOpen = e.target.open));
  list.querySelectorAll("[data-menu-eat]").forEach((btn) =>
    btn.addEventListener("click", () =>
      run(async () => {
        btn.disabled = true;
        const servings = Number(list.querySelector(`[data-menu-servings="${btn.dataset.menuEat}"]`).value);
        try {
          await api(`/menu/${btn.dataset.menuEat}/eat`, { method: "POST", body: { servings } });
          mealListOpen = true; // 入ったものを その場で確かめられるように 開く
        } finally {
          btn.disabled = false;
        }
        await render();
      }),
    ),
  );
  list.querySelectorAll("[data-menu-favorite]").forEach((btn) =>
    btn.addEventListener("click", () =>
      run(async () => {
        const m = data.menu.find((x) => x.id === Number(btn.dataset.menuFavorite));
        await api(`/menu/${m.id}/favorite`, { method: "PUT", body: { favorite: !m.favorite } });
        await render();
      }),
    ),
  );
  list.querySelectorAll("[data-menu-edit]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const m = data.menu.find((x) => x.id === Number(btn.dataset.menuEdit));
      mealDraft = { items: m.items.map((i) => ({ ...i })), questions: [], comment: "", source: "manual", menuId: m.id, menuName: m.name };
      renderMeals(data);
      window.scrollTo(0, 0);
    }),
  );
  list.querySelectorAll("[data-menu-delete]").forEach((btn) =>
    confirmTwice(btn, "本当に消す？", () =>
      run(async () => {
        await api(`/menu/${btn.dataset.menuDelete}`, { method: "DELETE" });
        await render();
      }),
    ),
  );
}

function draftSection() {
  const d = mealDraft;
  const rows = d.items
    .map(
      (item, i) => `<div class="draft-item" data-index="${i}">
        <div class="row">
          <input data-field="name" class="grow" maxlength="40" value="${esc(item.name)}" placeholder="料理・食品の名前">
          <button type="button" class="small danger" data-remove="${i}" aria-label="消す">×</button>
        </div>
        <input data-field="amount" maxlength="40" value="${esc(item.amount)}" placeholder="量（例：茶碗1杯・150g）" style="width:100%">
        <small class="hint draft-status" data-status="${i}">${item.status ?? ""}</small>
        <div class="draft-numbers">
          <label><span>kcal</span><input type="number" data-field="kcal" step="1" min="0" inputmode="decimal" value="${item.kcal}"></label>
          <label><span>たんぱく</span><input type="number" data-field="protein" step="0.1" min="0" inputmode="decimal" value="${item.protein}"></label>
          <label><span>脂質</span><input type="number" data-field="fat" step="0.1" min="0" inputmode="decimal" value="${item.fat}"></label>
          <label><span>炭水化物</span><input type="number" data-field="carbs" step="0.1" min="0" inputmode="decimal" value="${item.carbs}"></label>
        </div>
      </div>`,
    )
    .join("");
  const questions = d.questions.length
    ? `<div class="draft-questions">
        <h2>AIからの質問</h2>
        ${d.questions.map((q, i) => `<label>${esc(q)}<input data-answer="${i}" maxlength="60" placeholder="答え（空のままでもOK）"></label>`).join("")}
        <button type="button" id="draft-reanalyze">答えて 見積もり直す</button>
      </div>`
    : "";
  return `<section class="window">
    <h2>${
      d.menuId ? "マイメニューを直す" : d.source === "manual" ? "自分で入力" : d.source === "label" ? "栄養成分表示から読み取った" : "AIの見積もり"
    }（確かめて、ちがうところは直してね）</h2>
    ${d.comment ? `<p class="hint" style="margin-top:0">${esc(d.comment)}</p>` : ""}
    ${questions}
    <div id="draft-items">${rows}</div>
    <button type="button" id="draft-add" class="small" style="margin-top:8px">＋品目を足す</button>
    <p class="hint">名前や量を変えると、数字も自動で直るよ（量が「200g → 300g」のように数字なら その割合で、ちがうときは AIで計算）。</p>
    <p class="draft-total" id="draft-total"></p>
    ${
      d.menuId
        ? `<label class="menu-register"><span>名前</span><input id="draft-menu-name" class="grow" maxlength="40" value="${esc(d.menuName ?? "")}"></label>`
        : `<label class="menu-register"><input type="checkbox" id="draft-menu-check" ${d.menuCheck ? "checked" : ""}>
             <span>マイメニューにも登録</span>
             <input id="draft-menu-name" class="grow" maxlength="40" placeholder="名前（例：オイコス）" value="${esc(d.menuName ?? "")}"></label>`
    }
    <div class="row" style="margin-top:8px">
      <button type="button" id="draft-save" class="primary grow">${d.menuId ? "この内容で保存" : "記録する"}</button>
      <button type="button" id="draft-cancel" class="small danger">やめる</button>
    </div>
  </section>`;
}

function mealListSection(data) {
  const items = data.meals.length
    ? data.meals
        .map(
          (m) => `<li>
            <div class="row" style="justify-content:space-between">
              <b>${hhmm(m.eaten_at)}　${m.kcal}kcal</b>
              <span class="row" style="gap:6px">
                ${m.source === "menu" ? "" : `<button class="small" data-meal-register="${m.id}">登録</button>`}
                <button class="small danger" data-meal-delete="${m.id}">消す</button>
              </span>
            </div>
            <div>${esc(m.title)}</div>
            <small class="hint">たんぱく質 ${m.protein}g・脂質 ${m.fat}g・炭水化物 ${m.carbs}g</small>
          </li>`,
        )
        .join("")
    : '<li class="empty">今日はまだ記録がありません</li>';
  const history = data.history.length
    ? `<h2 style="margin-top:14px">これまで（最近2週間）</h2>
       <ul class="list">${data.history
         .map((h) => `<li class="history-line"><span>${dayLabel(h.day)}</span><span>${h.kcal}kcal／たんぱく質 ${h.protein}g・脂質 ${h.fat}g</span></li>`)
         .join("")}</ul>`
    : "";
  return `<section class="window">
    <details class="fold" id="meal-list" ${mealListOpen ? "open" : ""}>
      <summary><h2>今日 食べたもの（${data.totals.count}回・${data.totals.kcal}kcal）</h2></summary>
      <ul class="list">${items}</ul>
    </details>
    ${history}
  </section>`;
}

function readDraft() {
  const menuName = document.getElementById("draft-menu-name");
  if (menuName) mealDraft.menuName = menuName.value;
  const menuCheck = document.getElementById("draft-menu-check");
  if (menuCheck) mealDraft.menuCheck = menuCheck.checked;
  document.querySelectorAll(".draft-item").forEach((row) => {
    const item = mealDraft.items[Number(row.dataset.index)];
    row.querySelectorAll("[data-field]").forEach((input) => {
      const key = input.dataset.field;
      item[key] = key === "name" || key === "amount" ? input.value : Number(input.value) || 0;
    });
  });
}

// ---- 品目の名前や量を変えたら、数字も直す ----
// basis：今の数字が どの量の分か（量を変えたときに、ここから 割合で直す）
const UNIT_RE = /(\d+(?:\.\d+)?)\s*(kg|g|ml|mL|l|L|個|本|枚|杯|切れ|切|袋|玉|皿|パック|缶|貫|粒|尾|切れ)/g;

function amountParts(text) {
  const toBase = (n, unit) => (unit === "kg" || unit.toLowerCase() === "l" ? n * 1000 : n);
  const unitKey = (unit) => (unit === "kg" ? "g" : unit.toLowerCase() === "l" || unit.toLowerCase() === "ml" ? "ml" : unit === "切" ? "切れ" : unit);
  return [...String(text).normalize("NFKC").matchAll(UNIT_RE)].map((m) => ({ value: toBase(Number(m[1]), m[2]), unit: unitKey(m[2]) }));
}

// 量の文字から 割合を出す（重さ・量 → 数（個・杯など）の順に 同じ単位で比べる）。比べられなければ null
function amountRatio(before, after) {
  const a = amountParts(before);
  const b = amountParts(after);
  for (const units of [["g", "ml"], null]) {
    for (const x of a) {
      if (units && !units.includes(x.unit)) continue;
      const y = b.find((p) => p.unit === x.unit);
      if (y && x.value > 0 && y.value > 0) return y.value / x.value;
    }
  }
  return null;
}

const NUMBER_FIELDS = ["kcal", "protein", "fat", "carbs"];
const snapshot = (item) => ({ amount: item.amount, ...Object.fromEntries(NUMBER_FIELDS.map((k) => [k, Number(item[k]) || 0])) });
let draftPending = 0; // AIで計算している途中の 品目の数

function drawDraftRow(i) {
  const item = mealDraft.items[i];
  const row = document.querySelector(`.draft-item[data-index="${i}"]`);
  if (!row) return;
  NUMBER_FIELDS.forEach((k) => (row.querySelector(`[data-field="${k}"]`).value = item[k]));
  row.querySelector('[data-field="amount"]').value = item.amount;
  row.querySelector(`[data-status="${i}"]`).textContent = item.status ?? "";
  const save = document.getElementById("draft-save");
  if (save) save.disabled = draftPending > 0;
  drawDraftTotal();
}

async function recalcItem(i, field) {
  const item = mealDraft.items[i];
  if (!item.name.trim()) return;
  // 量だけ変えて、前と同じ単位の数字があれば 割合で直す（AIを使わない）
  if (field === "amount" && item.basis) {
    const ratio = amountRatio(item.basis.amount, item.amount);
    if (ratio) {
      const round1 = (v) => Math.round(v * 10) / 10;
      NUMBER_FIELDS.forEach((k) => (item[k] = k === "kcal" ? Math.round(item.basis[k] * ratio) : round1(item.basis[k] * ratio)));
      item.basis = snapshot(item);
      item.status = `量に合わせて ×${Math.round(ratio * 100) / 100} にした`;
      return drawDraftRow(i);
    }
  }
  // それ以外（品目を足した・名前を変えた・量が数字で比べられない）は AIで この品目だけ 計算する
  if (!mealAi.available) return;
  const context = [mealNote, ...mealDraft.items.filter((x, j) => j !== i && x.name.trim()).map((x) => x.name)].join("、");
  const asked = `${item.name}|${item.amount}`;
  item.asked = asked;
  item.status = "AIで計算中…";
  draftPending++;
  drawDraftRow(i);
  try {
    const res = await api("/meals/estimate-item", { method: "POST", body: { name: item.name, amount: item.amount, context } });
    if (item.asked !== asked) return; // 計算している間に また変えた → 新しいほうを使う
    Object.assign(item, { amount: res.amount, ...Object.fromEntries(NUMBER_FIELDS.map((k) => [k, res[k]])) });
    item.basis = snapshot(item);
    item.status = "AIで計算した";
  } catch (err) {
    if (item.asked === asked) item.status = `計算できなかった（${err.message}）`;
  } finally {
    draftPending--;
    if (mealDraft?.items.includes(item)) drawDraftRow(mealDraft.items.indexOf(item));
  }
}

function drawDraftTotal() {
  const el = document.getElementById("draft-total");
  if (!el) return;
  const items = mealDraft.items;
  el.textContent = `合計：${Math.round(sumOf(items, "kcal"))}kcal／たんぱく質 ${sumOf(items, "protein")}g・脂質 ${sumOf(items, "fat")}g・炭水化物 ${sumOf(items, "carbs")}g`;
}

async function analyzeMeal(button, answers) {
  button.disabled = true;
  const label = button.textContent;
  button.textContent = "AIが見ている…";
  try {
    const body = { images: mealPhotos, note: mealNote };
    if (answers) Object.assign(body, { questions: mealDraft.questions, answers });
    const res = await api("/meals/analyze", { method: "POST", body });
    mealDraft = {
      items: res.items,
      questions: res.questions,
      comment: res.comment,
      source: res.kind === "label" ? "label" : mealPhotos.length ? "photo" : "text",
    };
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function renderMeals(data) {
  view.innerHTML = `${mealSummary(data)}${mealDraft ? draftSection() : `${mealAddSection(data)}${menuSection(data)}`}${mealListSection(data)}`;

  if (mealDraft) {
    mealAi = data.ai;
    mealDraft.items.forEach((item) => (item.basis ??= snapshot(item)));
    drawDraftTotal();
    document.getElementById("draft-save").disabled = draftPending > 0;
    const items = document.getElementById("draft-items");
    items.addEventListener("input", (e) => {
      readDraft();
      drawDraftTotal();
      // 数字を自分で直したら、それを「今の量の分」として覚える
      const row = e.target.closest(".draft-item");
      if (row && NUMBER_FIELDS.includes(e.target.dataset.field)) {
        const item = mealDraft.items[Number(row.dataset.index)];
        item.basis = snapshot(item);
        item.status = "";
      }
    });
    // 名前や量を 変え終わったら（入力欄を離れたら）、数字を直す
    items.addEventListener("change", (e) => {
      const field = e.target.dataset.field;
      if (field !== "name" && field !== "amount") return;
      readDraft();
      recalcItem(Number(e.target.closest(".draft-item").dataset.index), field);
    });
    items.querySelectorAll("[data-remove]").forEach((btn) =>
      btn.addEventListener("click", () => {
        readDraft();
        mealDraft.items.splice(Number(btn.dataset.remove), 1);
        renderMeals(data);
      }),
    );
    document.getElementById("draft-add").addEventListener("click", () => {
      readDraft();
      mealDraft.items.push({ name: "", amount: "", kcal: 0, protein: 0, fat: 0, carbs: 0 });
      renderMeals(data);
    });
    document.getElementById("draft-menu-check")?.addEventListener("change", (e) => {
      const name = document.getElementById("draft-menu-name");
      if (e.target.checked && !name.value.trim()) name.value = (mealDraft.items[0]?.name ?? "").slice(0, 40);
      readDraft();
    });
    document.getElementById("draft-menu-name")?.addEventListener("input", readDraft);
    document.getElementById("draft-reanalyze")?.addEventListener("click", (e) =>
      run(async () => {
        const answers = [...document.querySelectorAll("[data-answer]")].map((input) => input.value);
        await analyzeMeal(e.target, answers);
        await render();
      }),
    );
    document.getElementById("draft-cancel").addEventListener("click", () => {
      mealDraft = null;
      renderMeals(data);
    });
    document.getElementById("draft-save").addEventListener("click", (e) =>
      run(async () => {
        readDraft();
        const items = mealDraft.items
          .filter((i) => i.name.trim())
          .map((i) => ({ name: i.name, amount: i.amount, ...Object.fromEntries(NUMBER_FIELDS.map((k) => [k, i[k]])) }));
        if (!items.length) throw new Error("品目を1つ以上 入れてね");
        const menuName = (mealDraft.menuName ?? "").trim();
        if (mealDraft.menuId && !menuName) throw new Error("マイメニューの名前を入れてね");
        if (mealDraft.menuCheck && !menuName) throw new Error("マイメニューの名前を入れてね（登録しないなら チェックを外す）");
        e.target.disabled = true;
        try {
          if (mealDraft.menuId) {
            await api(`/menu/${mealDraft.menuId}`, { method: "PUT", body: { name: menuName, items } });
          } else {
            await api("/meals", {
              method: "POST",
              body: { items, note: mealNote, source: mealDraft.source, menu_name: mealDraft.menuCheck ? menuName : null },
            });
            mealListOpen = true; // 入ったものを その場で確かめられるように 開く
          }
        } finally {
          e.target.disabled = false;
        }
        const editedMenu = Boolean(mealDraft.menuId);
        mealDraft = null;
        if (!editedMenu) {
          mealPhotos = [];
          mealNote = "";
        }
        await render();
      }),
    );
  } else {
    const note = document.getElementById("meal-note");
    note.addEventListener("input", () => (mealNote = note.value));
    document.getElementById("meal-photos").addEventListener("change", (e) =>
      run(async () => {
        // 前に選んだ写真に 足していく（iPhone のカメラは 1回に1枚しか撮れないので）
        const room = data.max_images - mealPhotos.length;
        const files = [...e.target.files];
        const added = await Promise.all(files.slice(0, room).map(shrinkPhoto));
        mealPhotos = [...mealPhotos, ...added];
        renderMeals(data);
        if (files.length > room) await showMessages([`写真は ${data.max_images}枚までだよ。`]);
      }),
    );
    view.querySelectorAll("[data-photo-rotate]").forEach((btn) =>
      btn.addEventListener("click", () =>
        run(async () => {
          const i = Number(btn.dataset.photoRotate);
          mealPhotos[i] = await rotatePhoto(mealPhotos[i]);
          renderMeals(data);
        }),
      ),
    );
    view.querySelectorAll("[data-photo-remove]").forEach((btn) =>
      btn.addEventListener("click", () => {
        mealPhotos.splice(Number(btn.dataset.photoRemove), 1);
        renderMeals(data);
      }),
    );
    document.getElementById("meal-form").addEventListener("submit", (e) => {
      e.preventDefault();
      if (!mealPhotos.length && !mealNote.trim()) return showMessages(["写真を選ぶか、食べたものを書いてね。"]);
      run(async () => {
        await analyzeMeal(document.getElementById("meal-analyze"));
        await render();
      });
    });
    document.getElementById("meal-manual").addEventListener("click", () => {
      mealDraft = { items: [{ name: mealNote.trim().slice(0, 40), amount: "", kcal: 0, protein: 0, fat: 0, carbs: 0 }], questions: [], comment: "", source: "manual" };
      renderMeals(data);
    });
  }

  document.getElementById("meal-list")?.addEventListener("toggle", (e) => (mealListOpen = e.target.open));
  bindMenuList(data);
  // 検索：入力するたびに 一覧だけ描き直す（入力欄は そのままなので 文字を打ち続けられる）
  document.getElementById("menu-search")?.addEventListener("input", (e) => {
    menuQuery = e.target.value;
    document.getElementById("menu-list").innerHTML = menuListHtml(data);
    bindMenuList(data);
  });
  // 記録した食事を マイメニューに登録する：ボタンを 名前の入力欄に変える
  view.querySelectorAll("[data-meal-register]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const meal = data.meals.find((x) => x.id === Number(btn.dataset.mealRegister));
      const li = btn.closest("li");
      li.insertAdjacentHTML(
        "beforeend",
        `<form class="row register-form" style="margin-top:6px">
           <input name="name" class="grow" maxlength="40" value="${esc(meal.title.slice(0, 40))}" placeholder="マイメニューの名前">
           <button class="small">登録する</button>
         </form>`,
      );
      btn.remove();
      const form = li.querySelector(".register-form");
      form.querySelector("input").select();
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        run(async () => {
          await api(`/menu/from-meal/${meal.id}`, { method: "POST", body: { name: new FormData(form).get("name") } });
          await showMessages(["マイメニューに登録した！ 次からは「食べた」を押すだけで記録できるよ。"]);
          await render();
        });
      });
    }),
  );

  view.querySelectorAll("[data-meal-delete]").forEach((btn) =>
    confirmTwice(btn, "本当に消す？", () =>
      run(async () => {
        await api(`/meals/${btn.dataset.mealDelete}`, { method: "DELETE" });
        await render();
      }),
    ),
  );
}

// ---- ログイン ----
function renderLogin() {
  clearInterval(timer);
  stopRest();
  logoutBtn.hidden = true;
  tabsEl.hidden = true;
  hidePartner();
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
      <p class="hint push-line" id="push-line" hidden></p>
      ${
        data.bmr
          ? `<p class="hint">今の基礎代謝（目安）：<b class="bmr-value">${data.bmr.value}kcal</b>／日（体重 ${data.bmr.weight}kg${
              data.bmr.body_fat ? `・体脂肪率 ${data.bmr.body_fat}%` : ""
            } から計算）</p>`
          : ""
      }
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

  drawPushLine();
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
          <details class="fold">
            <summary>
              <b>${dayLabel(w.day)}</b>
              <small class="hint">${hhmm(w.started_at)}〜${hhmm(w.ended_at)}・${w.minutes}分${w.body_weight ? `・${w.body_weight}kg` : ""}${
                w.kcal !== null ? `・約${w.kcal}kcal` : ""
              }</small>
            </summary>
          ${w.exercises
            .map(
              (g) => `<div class="history-line"><span>${esc(g.name)}${g.kcal ? `<br><small class="kcal">約${g.kcal}kcal${g.warmup ? `（アップ${g.warmup}セット込み）` : ""}</small>` : ""}</span><span>${g.sets
                .map((s) => (s.is_pr ? `<span class="ok">★${esc(s.label)}</span>` : esc(s.label)))
                .join("、")}</span></div>`,
            )
            .join("")}
          ${w.kcal !== null ? `<div class="kcal">消費カロリー 約${w.kcal}kcal</div>` : ""}
          ${w.memo ? `<small class="hint">${esc(w.memo)}</small>` : ""}
          </details>
        </li>`,
        )
        .join("")
    : '<li class="empty">まだ記録がありません</li>';
  return `<section class="window"><h2>筋トレの履歴（最近90日）</h2><ul class="list">${items}</ul>
    <p class="hint">日付を押すと、種目とセットが開くよ。グラフや経験値は、パソコンのハビットモンスターで見られる。</p></section>`;
}

// ---- 記録中の画面 ----
function renderActive(w, ex) {
  const exercises = ex.exercises.filter((e) => e.part === part);
  if (!exercises.some((e) => e.id === exerciseId)) exerciseId = exercises[0]?.id ?? null;
  // 種目を変えたとき：今日まだ1セットもやっていない種目なら、休憩タイマーを片づける
  // （準備やアップの間に 鳴らないように。1セット目で また始まる）。
  // もうセットをやった種目に戻るとき（スーパーセットなど）は、タイマーを そのままにする
  if (exerciseChanged) {
    exerciseChanged = false;
    if (rest && rest.exerciseId !== exerciseId && !w.exercises.some((g) => g.exercise_id === exerciseId)) stopRest();
  }
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
      <p class="hint">カロリーは、セットを追加した時刻の間（休憩＋動いている時間）で数えるよ。1セット終わるごとに「追加」を押すと正確になる。${
        plannedEnd ? "あとから記録のときは、決まった長さ（ふつう1.5分・ビッグ3は2.5分）で数える。" : ""
      }</p>
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
      exerciseChanged = true;
      renderActive(w, ex);
    }),
  );
  view.querySelector('select[name="exercise"]').addEventListener("change", (e) => {
    exerciseId = Number(e.target.value);
    exerciseChanged = true;
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
      exerciseChanged = true;
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
    tabsEl.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("selected", b.dataset.tab === tab));
    if (tab === "meal") {
      const meals = await api("/meals");
      logoutBtn.hidden = false;
      tabsEl.hidden = false;
      loadPartner();
      return renderMeals(meals);
    }
    const data = await api("/workouts");
    logoutBtn.hidden = false;
    tabsEl.hidden = false;
    loadPartner();
    loadPush();
    if (!data.current) {
      if (rest) stopRest(); // 筋トレが終わっていたら タイマーも片づける
      return renderStart(data);
    }
    renderActive(data.current, await api("/exercises"));
  });
}

tab = store.get(TAB_KEY) === "meal" ? "meal" : "workout";
restoreRest();
render();
