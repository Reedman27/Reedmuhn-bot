// Shared destructive-action confirmation.
//
// The page ships a strict `default-src 'self'` CSP with no 'unsafe-inline',
// which silently drops inline onsubmit="return confirm(...)" and
// onclick="return confirm(...)" attributes in every current browser - the
// form just submits (or the click just fires) with no dialog and no
// console error unless devtools happens to be open to the right tab. That
// meant every "are you sure?" prompt in the app (lockdown lift, mass
// timeout, invite revoke, purge, delete snapshot, end giveaway, etc.) was
// silently disabled: destructive actions executed instantly.
//
// Fix: put the confirmation message in a data-confirm="..." attribute
// (a plain data attribute, unaffected by the CSP) on the <form>, and wire
// it up here with a single delegated listener instead of one inline
// handler per form. Using the capture phase and checking the event
// target's own attribute (not currentTarget) means this keeps working for
// any form added to the page later without needing new listeners.
(function () {
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || !form.hasAttribute || !form.hasAttribute('data-confirm')) return;
        if (!window.confirm(form.getAttribute('data-confirm'))) {
            e.preventDefault();
        }
    }, true);
})();
