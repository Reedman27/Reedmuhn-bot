// Reaction roles emoji picker. Kept in a static file (rather than an
// inline <script> block) because the dashboard's Content-Security-Policy
// is default-src 'self' with no 'unsafe-inline' - inline scripts are
// blocked outright, same-origin static files are not.
(function () {
    var EMOJI = [
        "✅", "❌", "❓", "❗", "⭐", "🔥", "💯", "🎉", "🎮", "🎨",
        "🎵", "🎬", "📚", "📸", "⚽", "🏀", "🏆", "🎲", "🧩", "🛡️",
        "⚔️", "🗡️", "🏹", "🔮", "💎", "🌙", "☀️", "🌈", "❤️", "🧡",
        "💛", "💚", "💙", "💜", "🖤", "🤍", "👍", "👎", "👋", "🙌",
        "🙏", "💪", "🐱", "🐶", "🐉", "🦊", "🐺", "🦉", "🐝", "🦋",
    ];
    var picker = document.getElementById("emoji-picker");
    var input = document.getElementById("emoji");
    if (!picker || !input) return;

    EMOJI.forEach(function (e) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = e;
        btn.addEventListener("click", function () {
            input.value = e;
            picker.querySelectorAll("button.selected").forEach(function (b) { b.classList.remove("selected"); });
            btn.classList.add("selected");
        });
        picker.appendChild(btn);
    });
})();
