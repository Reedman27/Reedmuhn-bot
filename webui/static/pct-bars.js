// Percentage-bar widths (poll results, per-day analytics bars).
//
// These bars used a literal style="width: {{ pct }}%" attribute rendered
// by Jinja, which the page's `default-src 'self'` CSP (no 'unsafe-inline')
// silently drops - so the bars rendered with zero width/no fill and no
// console error unless devtools was already open. Per-property CSSOM
// writes like el.style.width = '...' are NOT "inline style" for CSP
// purposes and go through untouched in every current browser (same
// reasoning theme.js uses for the custom-theme colors), so the percentage
// is passed through a data-pct attribute instead and applied here.
(function () {
    document.querySelectorAll('[data-pct]').forEach(function (el) {
        var pct = parseFloat(el.getAttribute('data-pct'));
        if (!isFinite(pct)) return;
        el.style.width = pct + '%';
    });
})();
