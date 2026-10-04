// The bot's song card links to https://t.me/<bot>/<app>?startapp=<YouTube video id>; Telegram
// opens this page inside the app with the id as tgWebAppStartParam. The page shows that video in
// the official YouTube embed and nothing else. Telegram also passes the viewer's name and id
// (initData); this page never reads or sends them anywhere.
(function () {
  "use strict";
  var VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
  var tg = window.Telegram && window.Telegram.WebApp;
  var query = new URLSearchParams(window.location.search);
  // ?v=<id> lets the owner check the page in an ordinary browser.
  var id = query.get("tgWebAppStartParam") || (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) ||
           query.get("v") || "";
  var box = document.getElementById("player");

  function say(text) {
    var p = document.createElement("p");
    p.textContent = text;
    box.replaceChildren(p);
  }

  if (!VIDEO_ID.test(id)) {
    say("Видео не найдено. Открой плеер кнопкой «▶️ Плеер» под песней в чате.");
  } else {
    var iframe = document.createElement("iframe");
    iframe.src = "https://www.youtube-nocookie.com/embed/" + id + "?autoplay=1&playsinline=1&rel=0";
    iframe.title = "YouTube";
    iframe.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
    iframe.referrerPolicy = "strict-origin-when-cross-origin";
    var note = document.createElement("p");
    var link = document.createElement("a");
    link.href = "https://www.youtube.com/watch?v=" + id;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Не играет? Открыть на YouTube";
    link.addEventListener("click", function (event) {
      if (tg && tg.openLink) {    // outside the Mini App, not inside its window
        event.preventDefault();
        tg.openLink(link.href);
      }
    });
    note.appendChild(link);
    box.replaceChildren(iframe, note);
  }
  if (tg) {
    tg.ready();
    tg.expand();
  }
})();
