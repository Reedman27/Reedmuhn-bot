// Layout bootstrap.
//
// Loaded synchronously in <head>, before the stylesheet (same pattern as
// theme.js) so the saved layout choice is applied to <html data-layout>
// before first paint - no flash of the wrong layout. This used to be an
// inline <script> block, but the page ships a strict `default-src 'self'`
// CSP with no 'unsafe-inline', which silently drops inline <script>
// blocks in every current browser (no console error unless devtools is
// already open to the right tab), so it has to live in its own file.
(function () {
    if ('scrollRestoration' in history) { history.scrollRestoration = 'manual'; }
    try {
        var savedLayout = localStorage.getItem('reedmuhn-layout');
        document.documentElement.dataset.layout = (savedLayout === 'sidebar' || savedLayout === 'topnav') ? savedLayout : 'topnav';
    } catch (e) { document.documentElement.dataset.layout = 'topnav'; }
})();
