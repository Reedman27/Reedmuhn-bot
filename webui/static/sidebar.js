(() => {
  // Some browsers preserve/restore the previous page's scroll position on
  // ordinary navigations (not just back/forward) once history.scrollRestoration
  // is left on 'auto' - most noticeable after a form POST redirects back to a
  // page you'd scrolled down on. history.scrollRestoration is already set to
  // 'manual' inline in <head> (before first paint); this is the belt-and-
  // suspenders reset for browsers that still don't land at the top.
  window.scrollTo(0, 0);
})();

(() => {
  const key='reedmuhn.sidebar.groups';
  let state={}; try { state=JSON.parse(localStorage.getItem(key)||'{}'); } catch(e) {}
  document.querySelectorAll('.nav-group').forEach(group => {
    const id=group.dataset.group, button=group.querySelector('.nav-group-title');
    const items=group.querySelector('.nav-group-items');
    const active=!!group.querySelector('a.active');
    const open=active || state[id] !== false;
    group.classList.toggle('collapsed',!open); button.setAttribute('aria-expanded',String(open));
    button.addEventListener('click',()=>{ const next=group.classList.toggle('collapsed')===false; button.setAttribute('aria-expanded',String(next)); state[id]=next; localStorage.setItem(key,JSON.stringify(state)); });
  });
})();

// Ctrl+K jumps straight to the Search page (which searches both pages and
// content - see search.html). No visible button for this; it's a quiet
// shortcut for people who want it, not a second search UI to maintain.
(() => {
  const searchLink = document.querySelector('#sidebar-nav a[href$="/search"]');
  if (!searchLink) return;
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      location.href = searchLink.href;
    }
  });
})();

// Top-nav dropdown menus (click to open, click outside or Escape to close;
// only one open at a time).
(() => {
  const dropdowns = document.querySelectorAll('.topnav-dropdown');
  if (!dropdowns.length) return;

  function closeAll(except) {
    dropdowns.forEach(d => { if (d !== except) d.classList.remove('open'); });
  }

  dropdowns.forEach(dropdown => {
    const btn = dropdown.querySelector('.topnav-dropdown-btn');
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const willOpen = !dropdown.classList.contains('open');
      closeAll();
      dropdown.classList.toggle('open', willOpen);
    });
  });

  document.addEventListener('click', () => closeAll());
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeAll(); });
})();

// Layout switch: classic sidebar vs. top nav. Persisted in localStorage and
// applied to <html data-layout> before first paint (see base.html's <head>)
// so there's no flash of the other layout. This just wires up the button
// label/click - the actual visual switch is pure CSS keyed off the attribute.
(() => {
  const btn = document.getElementById('layout-toggle');
  if (!btn) return;
  const KEY = 'reedmuhn-layout';

  function label(layout) {
    return layout === 'sidebar' ? '☰ Try the new layout' : '📐 Switch to classic sidebar';
  }

  function refresh() {
    btn.textContent = label(document.documentElement.dataset.layout);
  }

  btn.addEventListener('click', () => {
    const next = document.documentElement.dataset.layout === 'sidebar' ? 'topnav' : 'sidebar';
    document.documentElement.dataset.layout = next;
    try { localStorage.setItem(KEY, next); } catch (e) { /* layout still applies for this page view */ }
    refresh();
  });

  refresh();
})();
