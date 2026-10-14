// ロック画面への通知を 受け取る係（アプリを閉じていても 動く）。画面のファイルは ここでは ためこまない。
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let data = {};
  try {
    data = e.data.json();
  } catch {
    // 中身が読めなくても、知らせだけは出す
  }
  e.waitUntil(
    self.registration.showNotification(data.title || "ハビモン筋トレ", {
      body: data.body || "",
      tag: data.tag || "rest", // 同じ種類の通知は 1つにまとめる
      renotify: true,
      icon: "/icon-512.png",
    }),
  );
});

// 通知を押したら アプリを開く
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => (list.length ? list[0].focus() : self.clients.openWindow("/"))),
  );
});
