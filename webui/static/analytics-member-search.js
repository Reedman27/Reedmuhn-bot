// Live member-search filter on the analytics member table.
//
// Externalized from an inline <script> block (blocked by this app's
// strict CSP - see confirm.js for the full explanation). The inline
// version embedded a server-rendered {{ members|length }} value directly
// in the script; rows.length is the same count computed client-side, so
// this version has no server-rendered value to worry about.
(function () {
    const input = document.getElementById("member-search");
    const rows = Array.from(document.querySelectorAll("#member-table tbody tr"));
    const empty = document.getElementById("member-empty");
    const countLabel = document.getElementById("member-count");
    if (!input || !countLabel) return;
    const baseLabel = rows.length;
    input.addEventListener("input", function () {
        const q = input.value.trim().toLowerCase();
        let visible = 0;
        rows.forEach(function (row) {
            const match = !q || row.dataset.search.includes(q);
            row.style.display = match ? "" : "none";
            if (match) visible++;
        });
        if (empty) empty.style.display = visible === 0 ? "" : "none";
        countLabel.textContent = q
            ? visible + " of " + baseLabel + " member" + (baseLabel !== 1 ? "s" : "")
            : baseLabel + " member" + (baseLabel !== 1 ? "s" : "");
    });
})();
