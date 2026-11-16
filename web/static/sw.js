/* Teslife Web Push worker. Shows a notice the server already wrote. */
self.addEventListener("push", function (event) {
  var data = { title: "Teslife", body: "", href: "/" };
  try {
    if (event.data) data = Object.assign(data, event.data.json());
  } catch (e) {}
  var title = String(data.title || "").replace(/^\s*(from\s+)?teslife\s*[:·\-]\s*/i, "");
  event.waitUntil(self.registration.showNotification(title || "알림", {
    body: data.body || "",
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    data: { href: data.href || "/", id: data.id || "", ts: data.ts || "" }
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var href = (event.notification.data && event.notification.data.href) || "/";
  var target = new URL(href, self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    var i;
    for (i = 0; i < list.length; i++) {
      if (list[i].url && list[i].navigate) return list[i].navigate(target).then(function (c) { return c && c.focus(); });
      if ("focus" in list[i]) return list[i].focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow(target);
  }));
});
