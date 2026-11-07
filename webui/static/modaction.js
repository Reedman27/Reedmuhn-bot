(() => {
    const actionSelect = document.getElementById('modaction_action');
    const durationFields = document.getElementById('modaction-duration-fields');
    const durationHint = document.getElementById('modaction-duration-hint');
    if (!actionSelect || !durationFields) return;

    // Kept in sync with MOD_TIMED_ACTIONS in webui/main.py.
    const TIMED_ACTIONS = new Set(['tempban', 'mute_role', 'timeout']);

    const update = () => {
        const timed = TIMED_ACTIONS.has(actionSelect.value);
        durationFields.style.display = timed ? '' : 'none';
        if (durationHint) {
            durationHint.textContent = actionSelect.value === 'timeout'
                ? 'Timeouts are capped at 28 days by Discord.'
                : 'Duration for this action.';
        }
    };
    actionSelect.addEventListener('change', update);
    update();
})();
