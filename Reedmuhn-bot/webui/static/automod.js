// Automod escalation tier form:
// - kick/ban don't take a duration, so hide (and stop requiring) the
//   duration fields unless a timed punishment is selected.
// - Timeouts are capped at 28 days by Discord; mutes/temp-bans up to 365
//   days. Once seconds/minutes/hours/days/weeks/months/years are all on
//   the table it's easy to accidentally type "6 months" for a timeout, so
//   this also caps the number input's `max` per unit and shows a live
//   hint of the largest value that unit allows for the chosen punishment.
//
// Kept in a static file (rather than an inline <script> block) because the
// dashboard's Content-Security-Policy is default-src 'self' with no
// 'unsafe-inline' - inline scripts are blocked outright, same-origin
// static files are not.
(function () {
    var TIMED_ACTIONS = { mute_role: true, timeout: true, tempban: true };
    var TIMEOUT_MAX_SECONDS = 28 * 86400;
    var DEFAULT_MAX_SECONDS = 365 * 86400;
    var UNIT_SECONDS = { s: 1, m: 60, h: 3600, d: 86400, w: 604800, mo: 30 * 86400, y: 365 * 86400 };
    var UNIT_LABELS = { s: "seconds", m: "minutes", h: "hours", d: "days", w: "weeks", mo: "months", y: "years" };

    var actionSelect = document.getElementById("tier_action");
    var durationFields = document.getElementById("tier-duration-fields");
    var durationValue = document.getElementById("tier_duration_value");
    var durationUnit = document.getElementById("tier_duration_unit");
    var durationHint = document.getElementById("tier-duration-hint");
    if (!actionSelect || !durationFields || !durationValue || !durationUnit) return;

    function maxSecondsForAction() {
        return actionSelect.value === "timeout" ? TIMEOUT_MAX_SECONDS : DEFAULT_MAX_SECONDS;
    }

    function syncMax() {
        var maxSeconds = maxSecondsForAction();
        var unitSeconds = UNIT_SECONDS[durationUnit.value] || 1;
        var maxForUnit = Math.max(1, Math.floor(maxSeconds / unitSeconds));
        durationValue.max = String(maxForUnit);
        if (durationValue.value && Number(durationValue.value) > maxForUnit) {
            durationValue.value = String(maxForUnit);
        }
        if (durationHint) {
            durationHint.textContent = "Up to " + maxForUnit + " " + UNIT_LABELS[durationUnit.value] + " for this punishment.";
        }
    }

    function sync() {
        var timed = !!TIMED_ACTIONS[actionSelect.value];
        durationFields.style.display = timed ? "" : "none";
        durationValue.required = timed;
        if (!timed) {
            durationValue.value = "";
            if (durationHint) durationHint.textContent = "";
            return;
        }
        syncMax();
    }

    actionSelect.addEventListener("change", sync);
    durationUnit.addEventListener("change", syncMax);
    sync();
})();
