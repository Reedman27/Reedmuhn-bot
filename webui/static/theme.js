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
//
// One theme isn't in style.css at all: "custom". Its 8 base colors are
// picked by the user in the theme editor (below) and stored as JSON in
// localStorage, then applied at runtime via
// documentElement.style.setProperty('--var', value) for each CSS custom
// property individually. That's deliberate, not incidental: the page ships
// a strict `style-src 'self'` CSP with no 'unsafe-inline', which blocks
// inline style="..." attributes and <style> blocks (and el.style.cssText,
// which is treated the same way) - but per-property CSSOM writes via
// setProperty()/the .style.<prop> setter aren't "inline style" for CSP
// purposes and go through untouched in every current browser. So the
// custom theme is built entirely with setProperty calls and DOM APIs
// (createElement, not innerHTML with style attributes) - never a style
// attribute string or a <style> tag - so it keeps working under the
// existing CSP without loosening it.
(function () {
    var STORAGE_KEY = "reedmuhn-theme";
    var CUSTOM_STORAGE_KEY = "reedmuhn-custom-theme";
    var CUSTOM_THEME_ID = "custom";
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
        { group: "Nature & soft", items: [
            { id: "catppuccin-green", label: "Catppuccin Green" },
            { id: "forest", label: "Forest" },
            { id: "lavender", label: "Lavender" },
            { id: "paper", label: "Paper" },
        ] },
        { group: "Monochrome", items: [
            { id: "midnight", label: "Midnight" },
            { id: "oled", label: "OLED" },
            { id: "high-contrast", label: "High Contrast" },
            { id: "obsidian", label: "Obsidian" },
        ] },
    ];

    // The 8 colors a custom theme editor lets someone pick directly.
    // --accent-dim, --accent-contrast and --shadow are derived from these
    // (see deriveVars below) rather than picked individually - every
    // built-in theme in style.css follows the same pattern (--accent-dim
    // is always the same shade as --border; --accent-contrast is always
    // whichever of the theme's light/dark text color reads on the accent),
    // so deriving them keeps a custom theme looking coherent without
    // asking someone to reason about 11 variables instead of 8.
    var EDITABLE_VARS = [
        { key: "bg", css: "--bg", label: "Background" },
        { key: "card", css: "--card", label: "Cards & panels" },
        { key: "sidebar", css: "--sidebar", label: "Sidebar" },
        { key: "text", css: "--text", label: "Text" },
        { key: "muted", css: "--muted", label: "Muted text" },
        { key: "accent", css: "--accent", label: "Accent" },
        { key: "border", css: "--border", label: "Borders" },
        { key: "danger", css: "--danger", label: "Danger" },
    ];

    var THEME_LABELS = {};
    THEMES.forEach(function (g) {
        g.items.forEach(function (t) { THEME_LABELS[t.id] = t.label; });
    });

    function currentTheme() {
        return document.documentElement.dataset.theme || DEFAULT_THEME;
    }

    function loadCustomPalette() {
        try {
            var raw = localStorage.getItem(CUSTOM_STORAGE_KEY);
            if (!raw) return null;
            var palette = JSON.parse(raw);
            var ok = EDITABLE_VARS.every(function (v) { return typeof palette[v.key] === "string"; });
            return ok ? palette : null;
        } catch (err) {
            return null;
        }
    }

    function saveCustomPalette(palette) {
        try {
            localStorage.setItem(CUSTOM_STORAGE_KEY, JSON.stringify(palette));
        } catch (err) {
            // Custom theme still applies for this page view even if it can't be saved.
        }
    }

    function clearCustomPalette() {
        try {
            localStorage.removeItem(CUSTOM_STORAGE_KEY);
        } catch (err) { /* nothing to clean up */ }
    }

    // ---- small color-math helpers (all pure, no DOM) ----

    function hexToRgb(hex) {
        hex = (hex || "").replace("#", "");
        if (hex.length === 3) hex = hex.split("").map(function (c) { return c + c; }).join("");
        var num = parseInt(hex, 16);
        if (isNaN(num) || hex.length !== 6) return { r: 128, g: 128, b: 128 };
        return { r: (num >> 16) & 255, g: (num >> 8) & 255, b: num & 255 };
    }

    function rgbCss(rgb, alpha) {
        return "rgba(" + rgb.r + ", " + rgb.g + ", " + rgb.b + ", " + alpha + ")";
    }

    // 0 (light, needs dark text) - 255 (dark, needs light text) brightness,
    // via the standard YIQ formula used for exactly this "what text color
    // reads on this background" question.
    function yiqBrightness(hex) {
        var rgb = hexToRgb(hex);
        return (rgb.r * 299 + rgb.g * 587 + rgb.b * 114) / 1000;
    }

    function idealTextColor(hex) {
        return yiqBrightness(hex) >= 140 ? "#1e1e2e" : "#ffffff";
    }

    // Every built-in theme keeps --accent-dim the same shade as --border,
    // --accent-contrast as whichever text color reads on the accent, and
    // --shadow as a black shadow on dark backgrounds or a shadow tinted
    // with the theme's own text color on light ones (see style.css) - this
    // reproduces that pattern from the 8 colors someone actually picks.
    function deriveVars(palette) {
        var bgIsDark = yiqBrightness(palette.bg) < 140;
        var shadow = bgIsDark
            ? "0 1px 2px rgba(0, 0, 0, 0.2), 0 6px 16px rgba(0, 0, 0, 0.3)"
            : "0 1px 2px " + rgbCss(hexToRgb(palette.text), 0.06) + ", 0 6px 16px " + rgbCss(hexToRgb(palette.text), 0.08);
        return {
            "--accent-dim": palette.border,
            "--accent-contrast": idealTextColor(palette.accent),
            "--shadow": shadow,
        };
    }

    function applyCustomVars(palette) {
        var root = document.documentElement.style;
        EDITABLE_VARS.forEach(function (v) { root.setProperty(v.css, palette[v.key]); });
        var derived = deriveVars(palette);
        Object.keys(derived).forEach(function (name) { root.setProperty(name, derived[name]); });
    }

    // Clears any custom-theme CSSOM overrides so a preset's own [data-theme]
    // CSS rule takes over cleanly instead of the previous custom values
    // lingering (inline CSSOM properties otherwise beat stylesheet rules).
    function clearCustomVars() {
        var root = document.documentElement.style;
        EDITABLE_VARS.forEach(function (v) { root.removeProperty(v.css); });
        ["--accent-dim", "--accent-contrast", "--shadow"].forEach(function (name) { root.removeProperty(name); });
    }

    // Reads a preset's actual colors straight from the CSS that already
    // defines them (via a throwaway element matched by [data-theme-preview])
    // instead of keeping a second copy in JS - same anti-drift reasoning as
    // the module docstring above.
    function readPresetPalette(themeId) {
        var probe = document.createElement("div");
        probe.setAttribute("data-theme-preview", themeId);
        probe.style.position = "absolute";
        probe.style.opacity = "0";
        probe.style.pointerEvents = "none";
        document.body.appendChild(probe);
        var computed = getComputedStyle(probe);
        var palette = {};
        EDITABLE_VARS.forEach(function (v) {
            palette[v.key] = computed.getPropertyValue(v.css).trim() || "#888888";
        });
        document.body.removeChild(probe);
        return palette;
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
    if (saved === CUSTOM_THEME_ID) {
        var earlyPalette = loadCustomPalette();
        if (earlyPalette) {
            document.documentElement.dataset.theme = CUSTOM_THEME_ID;
            applyCustomVars(earlyPalette);
        }
        // No saved palette despite the id being "custom" (cleared in another
        // tab, corrupted, etc.) - fall through and stay on the default theme
        // rather than showing an unstyled/mismatched page.
    } else if (saved && THEME_LABELS[saved]) {
        document.documentElement.dataset.theme = saved;
    }

    function makeSwatch(themeId, palette) {
        var swatch = document.createElement("span");
        swatch.className = "theme-swatch";
        var bg = document.createElement("i");
        bg.className = "sw sw-bg";
        var accent = document.createElement("i");
        accent.className = "sw sw-accent";
        if (palette) {
            // Custom theme has no [data-theme-preview] CSS rule to draw
            // from (its colors don't exist until someone picks them), so
            // set the two swatch dots directly - still per-property
            // setProperty calls, not a style attribute string, so this
            // stays clear of the style-src CSP restriction (see top of file).
            bg.style.setProperty("background-color", palette.bg);
            accent.style.setProperty("background-color", palette.accent);
        } else {
            swatch.dataset.themePreview = themeId;
        }
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
        var customGroupEl = null;
        var customOptionEl = null;
        var editorState = null; // set while the editor overlay is open

        function refreshButtonSwatch(theme) {
            buttonSwatch.innerHTML = "";
            if (theme === CUSTOM_THEME_ID) {
                var palette = loadCustomPalette();
                buttonSwatch.appendChild(makeSwatch(theme, palette));
            } else {
                buttonSwatch.dataset.themePreview = theme;
                var bg = document.createElement("i");
                bg.className = "sw sw-bg";
                var accent = document.createElement("i");
                accent.className = "sw sw-accent";
                buttonSwatch.appendChild(bg);
                buttonSwatch.appendChild(accent);
            }
        }

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

            // "Your theme" group - only shown once someone has actually
            // saved a custom palette. Rebuilt (not just toggled) whenever
            // the saved palette changes, so its swatch always matches.
            customGroupEl = document.createElement("div");
            customGroupEl.className = "theme-menu-group";
            customGroupEl.setAttribute("role", "group");
            customGroupEl.setAttribute("aria-label", "Your theme");
            customGroupEl.hidden = true;
            var customGroupLabel = document.createElement("div");
            customGroupLabel.className = "theme-menu-group-label";
            customGroupLabel.textContent = "Your theme";
            customGroupEl.appendChild(customGroupLabel);
            panel.insertBefore(customGroupEl, panel.firstChild);

            var createBtn = document.createElement("button");
            createBtn.type = "button";
            createBtn.id = "theme-menu-create-custom";
            createBtn.className = "theme-menu-create";
            createBtn.textContent = "🎨 Create custom theme…";
            createBtn.addEventListener("click", function () {
                closePanel();
                openEditor();
            });
            panel.appendChild(createBtn);
        }

        function refreshCustomOption() {
            var palette = loadCustomPalette();
            customGroupEl.innerHTML = "";
            var createBtn = document.getElementById("theme-menu-create-custom");
            if (!palette) {
                customGroupEl.hidden = true;
                delete optionButtons[CUSTOM_THEME_ID];
                if (createBtn) createBtn.textContent = "🎨 Create custom theme…";
                return;
            }
            customGroupEl.hidden = false;
            if (createBtn) createBtn.textContent = "🎨 Edit custom theme…";

            var customGroupLabel = document.createElement("div");
            customGroupLabel.className = "theme-menu-group-label";
            customGroupLabel.textContent = "Your theme";
            customGroupEl.appendChild(customGroupLabel);

            var opt = document.createElement("button");
            opt.type = "button";
            opt.className = "theme-menu-option";
            opt.setAttribute("role", "option");
            opt.dataset.theme = CUSTOM_THEME_ID;
            opt.appendChild(makeSwatch(CUSTOM_THEME_ID, palette));
            var name = document.createElement("span");
            name.className = "theme-menu-option-name";
            name.textContent = "Custom";
            opt.appendChild(name);
            var checkIcon = document.createElement("span");
            checkIcon.className = "theme-menu-option-check";
            checkIcon.textContent = "✓";
            opt.appendChild(checkIcon);
            opt.addEventListener("click", function () {
                selectTheme(CUSTOM_THEME_ID);
                closePanel();
                button.focus();
            });
            customOptionEl = opt;
            optionButtons[CUSTOM_THEME_ID] = opt;
            customGroupEl.appendChild(opt);
        }

        function refreshSelection() {
            var theme = currentTheme();
            var label = theme === CUSTOM_THEME_ID ? "Custom" : (THEME_LABELS[theme] || THEME_LABELS[DEFAULT_THEME]);
            currentLabel.textContent = label;
            refreshButtonSwatch(theme);
            Object.keys(optionButtons).forEach(function (id) {
                var isSelected = id === theme;
                optionButtons[id].classList.toggle("is-selected", isSelected);
                optionButtons[id].setAttribute("aria-selected", isSelected ? "true" : "false");
            });
        }

        function selectTheme(themeId) {
            if (themeId === CUSTOM_THEME_ID) {
                var palette = loadCustomPalette();
                if (!palette) return; // shouldn't happen - option only exists when a palette is saved
                document.documentElement.dataset.theme = CUSTOM_THEME_ID;
                applyCustomVars(palette);
            } else {
                if (!THEME_LABELS[themeId]) return;
                clearCustomVars();
                document.documentElement.dataset.theme = themeId;
            }
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

        // ---- custom theme editor overlay ----

        function openEditor() {
            var startingTheme = currentTheme();
            var startingPalette = startingTheme === CUSTOM_THEME_ID ? loadCustomPalette() : null;
            var draft = startingPalette
                ? Object.assign({}, startingPalette)
                : readPresetPalette(startingTheme === CUSTOM_THEME_ID ? DEFAULT_THEME : startingTheme);

            editorState = { startingTheme: startingTheme, startingPalette: startingPalette };

            var overlay = document.createElement("div");
            overlay.className = "theme-editor-overlay";
            overlay.id = "theme-editor-overlay";

            var dialog = document.createElement("div");
            dialog.className = "theme-editor";
            dialog.setAttribute("role", "dialog");
            dialog.setAttribute("aria-modal", "true");
            dialog.setAttribute("aria-label", "Custom theme editor");

            var heading = document.createElement("h2");
            heading.className = "theme-editor-title";
            heading.textContent = "Custom theme";
            dialog.appendChild(heading);

            var startFromRow = document.createElement("label");
            startFromRow.className = "theme-editor-startfrom";
            startFromRow.textContent = "Start from a preset";
            var startFromSelect = document.createElement("select");
            THEMES.forEach(function (group) {
                var optgroup = document.createElement("optgroup");
                optgroup.label = group.group;
                group.items.forEach(function (theme) {
                    var opt = document.createElement("option");
                    opt.value = theme.id;
                    opt.textContent = theme.label;
                    optgroup.appendChild(opt);
                });
                startFromSelect.appendChild(optgroup);
            });
            startFromSelect.value = THEME_LABELS[startingTheme] ? startingTheme : DEFAULT_THEME;
            startFromSelect.addEventListener("change", function () {
                var preset = readPresetPalette(startFromSelect.value);
                EDITABLE_VARS.forEach(function (v) {
                    draft[v.key] = preset[v.key];
                    inputs[v.key].colorInput.value = preset[v.key];
                    inputs[v.key].textInput.value = preset[v.key];
                });
                applyCustomVars(draft);
            });
            startFromRow.appendChild(startFromSelect);
            dialog.appendChild(startFromRow);

            var fields = document.createElement("div");
            fields.className = "theme-editor-fields";
            var inputs = {};

            EDITABLE_VARS.forEach(function (v) {
                var row = document.createElement("div");
                row.className = "theme-editor-field";

                var label = document.createElement("label");
                label.textContent = v.label;
                label.setAttribute("for", "theme-editor-" + v.key);
                row.appendChild(label);

                var colorInput = document.createElement("input");
                colorInput.type = "color";
                colorInput.id = "theme-editor-" + v.key;
                colorInput.value = /^#[0-9a-fA-F]{6}$/.test(draft[v.key]) ? draft[v.key] : "#888888";

                var textInput = document.createElement("input");
                textInput.type = "text";
                textInput.className = "theme-editor-hex";
                textInput.value = draft[v.key];
                textInput.setAttribute("aria-label", v.label + " hex value");
                textInput.maxLength = 7;

                function apply(value) {
                    draft[v.key] = value;
                    applyCustomVars(draft);
                }

                colorInput.addEventListener("input", function () {
                    textInput.value = colorInput.value;
                    apply(colorInput.value);
                });
                textInput.addEventListener("change", function () {
                    var value = textInput.value.trim();
                    if (!/^#[0-9a-fA-F]{6}$/.test(value)) {
                        textInput.value = draft[v.key];
                        return;
                    }
                    colorInput.value = value;
                    apply(value);
                });

                row.appendChild(colorInput);
                row.appendChild(textInput);
                fields.appendChild(row);
                inputs[v.key] = { colorInput: colorInput, textInput: textInput };
            });
            dialog.appendChild(fields);

            var actions = document.createElement("div");
            actions.className = "theme-editor-actions";

            if (startingPalette) {
                var deleteBtn = document.createElement("button");
                deleteBtn.type = "button";
                deleteBtn.className = "danger";
                deleteBtn.textContent = "Delete custom theme";
                deleteBtn.addEventListener("click", function () {
                    clearCustomPalette();
                    clearCustomVars();
                    document.documentElement.dataset.theme = DEFAULT_THEME;
                    try { localStorage.setItem(STORAGE_KEY, DEFAULT_THEME); } catch (err) { /* best effort */ }
                    refreshCustomOption();
                    refreshSelection();
                    closeEditor();
                });
                actions.appendChild(deleteBtn);
            }

            var spacer = document.createElement("span");
            spacer.className = "theme-editor-spacer";
            actions.appendChild(spacer);

            var cancelBtn = document.createElement("button");
            cancelBtn.type = "button";
            cancelBtn.className = "secondary";
            cancelBtn.textContent = "Cancel";
            cancelBtn.addEventListener("click", function () { closeEditor(true); });
            actions.appendChild(cancelBtn);

            var saveBtn = document.createElement("button");
            saveBtn.type = "button";
            saveBtn.textContent = "Save theme";
            saveBtn.addEventListener("click", function () {
                saveCustomPalette(draft);
                document.documentElement.dataset.theme = CUSTOM_THEME_ID;
                try { localStorage.setItem(STORAGE_KEY, CUSTOM_THEME_ID); } catch (err) { /* best effort */ }
                applyCustomVars(draft);
                refreshCustomOption();
                refreshSelection();
                closeEditor();
            });
            actions.appendChild(saveBtn);

            dialog.appendChild(actions);
            overlay.appendChild(dialog);
            document.body.appendChild(overlay);

            overlay.addEventListener("mousedown", function (event) {
                if (event.target === overlay) closeEditor(true);
            });
            document.addEventListener("keydown", editorKeydown);

            // Live-preview the starting palette immediately (matches
            // whatever's already applied when starting from the current
            // custom theme; switches to it when starting from a preset).
            applyCustomVars(draft);
            inputs[EDITABLE_VARS[0].key].colorInput.focus();
        }

        function editorKeydown(event) {
            if (event.key === "Escape") closeEditor(true);
        }

        function closeEditor(revert) {
            var overlay = document.getElementById("theme-editor-overlay");
            if (overlay) overlay.remove();
            document.removeEventListener("keydown", editorKeydown);
            if (revert && editorState) {
                if (editorState.startingTheme === CUSTOM_THEME_ID && editorState.startingPalette) {
                    document.documentElement.dataset.theme = CUSTOM_THEME_ID;
                    applyCustomVars(editorState.startingPalette);
                } else {
                    clearCustomVars();
                    document.documentElement.dataset.theme = editorState.startingTheme;
                }
            }
            editorState = null;
            refreshSelection();
        }

        buildPanel();
        refreshCustomOption();
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
