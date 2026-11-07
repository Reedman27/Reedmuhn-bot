// Channel Feed: converts the epoch-seconds timestamps the server sends into
// the viewer's local time, and (on the per-channel viewer page) polls for
// new messages so the page updates without a manual refresh.
//
// Kept in a static file, not an inline <script>, because the dashboard's
// CSP is default-src 'self' with no 'unsafe-inline'.
(function () {
    var POLL_INTERVAL_MS = 4000;

    function formatTs(seconds) {
        var d = new Date(seconds * 1000);
        return d.toLocaleString();
    }

    function applyTimestamps(root) {
        var nodes = root.querySelectorAll("[data-ts]");
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            var ts = parseInt(el.getAttribute("data-ts"), 10);
            if (!isNaN(ts)) el.textContent = formatTs(ts);
        }
    }

    function buildMessageEl(m) {
        var wrap = document.createElement("div");
        wrap.className = "feed-message";
        wrap.setAttribute("data-id", String(m.id));

        var avatar = document.createElement("img");
        avatar.className = "feed-avatar";
        avatar.src = m.author_avatar || "";
        avatar.alt = "";
        avatar.onerror = function () { avatar.style.visibility = "hidden"; };
        wrap.appendChild(avatar);

        var body = document.createElement("div");
        body.className = "feed-message-body";

        var head = document.createElement("div");
        head.className = "feed-message-head";
        var author = document.createElement("strong");
        author.textContent = m.author_name;
        var ts = document.createElement("span");
        ts.className = "muted feed-ts";
        ts.setAttribute("data-ts", String(m.created_at));
        ts.textContent = formatTs(m.created_at);
        head.appendChild(author);
        head.appendChild(document.createTextNode(" "));
        head.appendChild(ts);
        body.appendChild(head);

        var content = document.createElement("div");
        content.className = "feed-message-content";
        content.textContent = m.content;
        body.appendChild(content);

        (m.attachments || []).forEach(function (url) {
            var att = document.createElement("div");
            att.className = "feed-attachment";
            var a = document.createElement("a");
            a.href = url;
            a.target = "_blank";
            a.rel = "noopener";
            a.textContent = url;
            att.appendChild(a);
            body.appendChild(att);
        });

        wrap.appendChild(body);
        return wrap;
    }

    function startPolling(container) {
        var pollUrl = container.getAttribute("data-poll-url");
        var lastId = parseInt(container.getAttribute("data-last-id"), 10) || 0;
        if (!pollUrl) return;

        function tick() {
            fetch(pollUrl + "?after=" + lastId, { credentials: "same-origin" })
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (data) {
                    if (!data || !data.messages || !data.messages.length) return;
                    var emptyHint = container.querySelector("p.muted");
                    if (emptyHint) emptyHint.remove();
                    var nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
                    data.messages.forEach(function (m) {
                        container.appendChild(buildMessageEl(m));
                        lastId = Math.max(lastId, m.id);
                    });
                    container.setAttribute("data-last-id", String(lastId));
                    if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
                })
                .catch(function () { /* transient network hiccup - next tick retries */ });
        }

        setInterval(tick, POLL_INTERVAL_MS);
    }

    document.addEventListener("DOMContentLoaded", function () {
        applyTimestamps(document);

        // Server-rendered avatar images on the per-channel viewer page: an
        // event listener here (not an inline onerror="" attribute) because
        // the dashboard's CSP has no 'unsafe-inline' in script-src, which
        // silently blocks inline event handler attributes, not just inline
        // <script> blocks. The JS-built ones from buildMessageEl already
        // set .onerror as a property, which CSP doesn't restrict - only the
        // ones written directly in the template needed this.
        document.querySelectorAll(".feed-avatar").forEach(function (img) {
            img.addEventListener("error", function () {
                img.style.visibility = "hidden";
            });
        });

        var container = document.getElementById("feed-messages");
        if (container) startPolling(container);
    });
})();
