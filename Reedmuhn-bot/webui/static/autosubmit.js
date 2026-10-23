// Wires up any checkbox marked data-autosubmit="1" to submit its enclosing
// form as soon as it's toggled - the CSP-safe replacement for
// onchange="this.form.submit()", which the dashboard's CSP (no
// 'unsafe-inline' in script-src) silently drops. A silently-dropped inline
// handler looks identical to a working one until you check whether the
// setting actually persisted, so this pattern is shared across every
// instant-toggle checkbox rather than re-solved per page.
(function () {
    document.querySelectorAll('input[type="checkbox"][data-autosubmit]').forEach(function (box) {
        box.addEventListener("change", function () {
            if (box.form) box.form.requestSubmit ? box.form.requestSubmit() : box.form.submit();
        });
    });
})();
