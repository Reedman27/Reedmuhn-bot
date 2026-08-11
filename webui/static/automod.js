// Automod escalation tier form: kick/ban don't take a duration, so hide
// (and stop requiring) the duration fields unless a timed punishment is
// selected. Kept in a static file (rather than an inline <script> block)
// because the dashboard's Content-Security-Policy is default-src 'self'
// with no 'unsafe-inline' - inline scripts are blocked outright, same-origin
// static files are not.
(function () {
    var TIMED_ACTIONS = { mute_role: true, timeout: true, tempban: true };

    var actionSelect = document.getElementById("tier_action");
    var durationFields = document.getElementById("tier-duration-fields");
    var durationValue = document.getElementById("tier_duration_value");
    if (!actionSelect || !durationFields || !durationValue) return;

    function sync() {
        var timed = !!TIMED_ACTIONS[actionSelect.value];
        durationFields.style.display = timed ? "" : "none";
        durationValue.required = timed;
        if (!timed) durationValue.value = "";
    }

    actionSelect.addEventListener("change", sync);
    sync();
})();
