// Live character count for the Talk composer.
//
// Externalized from an inline <script> block (blocked by this app's
// strict CSP - see confirm.js for the full explanation).
(() => {
    const box = document.getElementById('content');
    const count = document.getElementById('talk-count');
    if (!box || !count) return;
    const update = () => { count.textContent = box.value.length; };
    box.addEventListener('input', update);
    update();
})();
