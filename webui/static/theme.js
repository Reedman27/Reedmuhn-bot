// Theme switcher.
//
// This file is loaded synchronously in <head>, before the stylesheet, so
// the saved theme is applied to <html data-theme> before first paint (no
// flash of the wrong theme). It also builds and wires up the grouped
// theme menu in the sidebar footer once the page has loaded, so picking a
// theme updates <html data-theme> and remembers the choice for next time.
//
// THEMES below is id/label/group text ONLY - no colors. Every entry's
// actual colors live in style.css as a [data-theme="<id>"] block (and a
// matching [data-theme-preview="<id>"] block used for the swatch here),
// so this list and the CSS can't drift out of sync with each other.
(function () {
    var STORAGE_KEY = "reedmuhn-theme";
    var DEFAULT_THEME = "mocha";

    var THEMES = [
        { group: "Catppuccin", items: [
            { id: "mocha", label: "Catppuccin Mocha" },
            { id: "macchiato", label: "Catppuccin Macchiato" },
            { id: "frappe", label: "Catppuccin Frappé" },
            { id: "latte", label: "Catppuccin Latte" },
        ] },
        { group: "Adriatic", items: [
            { id: "adriatic", label: "Adriatic" },
            { id: "adriatic-light", label: "Adriatic Light" },
        ] },
        { group: "Pastel", items: [
            { id: "pastel-dawn", label: "Pastel Dawn" },
            { id: "pastel-dusk", label: "Pastel Dusk" },
        ] },
        { group: "Nord & Dracula", items: [
            { id: "nord", label: "Nord" },
            { id: "dracula", label: "Dracula" },
        ] },
        { group: "Gruvbox", items: [
            { id: "gruvbox-dark", label: "Gruvbox Dark" },
            { id: "gruvbox-light", label: "Gruvbox Light" },
        ] },
        { group: "Solarized", items: [
            { id: "solarized-dark", label: "Solarized Dark" },
            { id: "solarized-light", label: "Solarized Light" },
        ] },
        { group: "Editor classics", items: [
            { id: "tokyo-night", label: "Tokyo Night" },
            { id: "one-dark", label: "One Dark" },
            { id: "monokai", label: "Monokai" },
        ] },
        { group: "Everforest", items: [
            { id: "everforest-dark", label: "Everforest Dark" },
            { id: "everforest-light", label: "Everforest Light" },
        ] },
        { group: "Aurora", items: [
            { id: "aurora", label: "Aurora" },
            { id: "aurora-night", label: "Aurora Night" },
        ] },
        { group: "Arctic", items: [
            { id: "arctic", label: "Arctic" },
            { id: "arctic-night", label: "Arctic Night" },
        ] },
        { group: "Ocean", items: [
            { id: "oceanic", label: "Oceanic" },
            { id: "deep-ocean", label: "Deep Ocean" },
        ] },
        { group: "Sunset", items: [
            { id: "sunset", label: "Sunset" },
            { id: "sunset-night", label: "Sunset Night" },
        ] },
        { group: "Rose", items: [
            { id: "rose", label: "Rose" },
            { id: "rose-night", label: "Rose Night" },
        ] },
        { group: "Neon & retro", items: [
            { id: "synthwave", label: "Synthwave" },
            { id: "terminal", label: "Terminal" },
            { id: "amber-terminal", label: "Amber Terminal" },
        ] },
        { group: "Royal", items: [
            { id: "royal", label: "Royal" },
        ] },
        { group: "Monochrome", items: [
            { id: "midnight", label: "Midnight" },
            { id: "oled", label: "OLED" },
            { id: "high-contrast", label: "High Contrast" },
            { id: "obsidian", label: "Obsidian" },
        ] },
    ];

    var THEME_LABELS = {};
    THEMES.forEach(function (g) {
        g.items.forEach(function (t) { THEME_LABELS[t.id] = t.label; });
    });

    function currentTheme() {
        return document.documentElement.dataset.theme || DEFAULT_THEME;
    }

    // Apply saved theme immediately (before DOMContentLoaded) to avoid a
    // flash of the wrong theme.
    var saved = null;
    try {
        saved = localStorage.getItem(STORAGE_KEY);
    } catch (err) {
        // localStorage can be unavailable (private browsing, etc.) - fall
        // back to the default theme for this page view.
    }
    if (saved && THEME_LABELS[saved]) {
        document.documentElement.dataset.theme = saved;
    }

    function makeSwatch(themeId) {
        var swatch = document.createElement("span");
        swatch.className = "theme-swatch";
        swatch.dataset.themePreview = themeId;
        var bg = document.createElement("i");
        bg.className = "sw sw-bg";
        var accent = document.createElement("i");
        accent.className = "sw sw-accent";
        swatch.appendChild(bg);
        swatch.appendChild(accent);
        return swatch;
    }

    document.addEventListener("DOMContentLoaded", function () {
        var menu = document.getElementById("theme-menu");
        var button = document.getElementById("theme-menu-button");
        var panel = document.getElementById("theme-menu-panel");
        var currentLabel = document.getElementById("theme-menu-current");
        var buttonSwatch = document.getElementById("theme-menu-swatch");
        if (!menu || !button || !panel || !currentLabel || !buttonSwatch) return;

        var optionButtons = {};

        function buildPanel() {
            THEMES.forEach(function (group) {
                var groupEl = document.createElement("div");
                groupEl.className = "theme-menu-group";
                groupEl.setAttribute("role", "group");
                groupEl.setAttribute("aria-label", group.group);

                var groupLabel = document.createElement("div");
                groupLabel.className = "theme-menu-group-label";
                groupLabel.textContent = group.group;
                groupEl.appendChild(groupLabel);

                group.items.forEach(function (theme) {
                    var opt = document.createElement("button");
                    opt.type = "button";
                    opt.className = "theme-menu-option";
                    opt.setAttribute("role", "option");
                    opt.dataset.theme = theme.id;

                    opt.appendChild(makeSwatch(theme.id));

                    var name = document.createElement("span");
                    name.className = "theme-menu-option-name";
                    name.textContent = theme.label;
                    opt.appendChild(name);

                    var checkIcon = document.createElement("span");
                    checkIcon.className = "theme-menu-option-check";
                    checkIcon.textContent = "✓";
                    opt.appendChild(checkIcon);

                    opt.addEventListener("click", function () {
                        selectTheme(theme.id);
                        closePanel();
                        button.focus();
                    });

                    optionButtons[theme.id] = opt;
                    groupEl.appendChild(opt);
                });

                panel.appendChild(groupEl);
            });
        }

        function refreshSelection() {
            var theme = currentTheme();
            var label = THEME_LABELS[theme] || THEME_LABELS[DEFAULT_THEME];
            currentLabel.textContent = label;
            buttonSwatch.dataset.themePreview = theme;
            Object.keys(optionButtons).forEach(function (id) {
                var isSelected = id === theme;
                optionButtons[id].classList.toggle("is-selected", isSelected);
                optionButtons[id].setAttribute("aria-selected", isSelected ? "true" : "false");
            });
        }

        function selectTheme(themeId) {
            if (!THEME_LABELS[themeId]) return;
            document.documentElement.dataset.theme = themeId;
            try {
                localStorage.setItem(STORAGE_KEY, themeId);
            } catch (err) {
                // Theme still applies for this page view even if it can't be saved.
            }
            refreshSelection();
        }

        function openPanel() {
            panel.hidden = false;
            button.setAttribute("aria-expanded", "true");
            var selected = optionButtons[currentTheme()];
            if (selected) selected.focus();
        }

        function closePanel() {
            panel.hidden = true;
            button.setAttribute("aria-expanded", "false");
        }

        function isOpen() {
            return !panel.hidden;
        }

        buildPanel();
        refreshSelection();

        button.addEventListener("click", function () {
            if (isOpen()) {
                closePanel();
            } else {
                openPanel();
            }
        });

        document.addEventListener("click", function (event) {
            if (isOpen() && !menu.contains(event.target)) closePanel();
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && isOpen()) {
                closePanel();
                button.focus();
            }
        });

        // Basic up/down arrow navigation between options while the panel is open.
        panel.addEventListener("keydown", function (event) {
            if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
            event.preventDefault();
            var focusable = Array.prototype.filter.call(
                panel.querySelectorAll(".theme-menu-option"),
                function (el) { return !el.disabled; }
            );
            var idx = focusable.indexOf(document.activeElement);
            if (idx === -1) idx = 0;
            var next = event.key === "ArrowDown" ? idx + 1 : idx - 1;
            if (next < 0) next = focusable.length - 1;
            if (next >= focusable.length) next = 0;
            focusable[next].focus();
        });
    });
})();
