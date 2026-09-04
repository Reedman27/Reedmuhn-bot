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

// Command-palette-style "jump to page" search. Builds its list straight from
// the sidebar's own links (plus a few extra keyword synonyms below), so a new
// page in base.html automatically becomes searchable without touching this file.
(() => {
  const trigger = document.getElementById('quickjump-trigger');
  const overlay = document.getElementById('quickjump-overlay');
  if (!trigger || !overlay) return;

  const input = document.getElementById('quickjump-input');
  const list = document.getElementById('quickjump-results');
  const empty = document.getElementById('quickjump-empty');

  // Extra search terms for pages whose feature name differs from its nav
  // label/icon (so e.g. typing "xp" or "leveling" finds the Extras page).
  const SYNONYMS = {
    '/leveling': 'xp leveling levels leaderboard',
    '/economy': 'coins balance daily pay richest money',
    '/giveaways': 'giveaway prize winners',
    '/counters': 'live counter member count',
    '/twitch': 'stream live notification',
    '/feeds': 'rss atom feed news',
    '/automod': 'spam filter bad words link blocking gif',
    '/moderation': 'warn warnings kick ban mute timeout tempban cases votekick vote kick history',
    '/moderationqueue': 'automod approvals pending review',
    '/rules': 'server rules',
    '/verification': 'gate captcha button role',
    '/emergency': 'lockdown panic mass timeout revoke invites',
    '/antinuke': 'anti nuke nuke protection webhook mass ban mass kick',
    '/raid': 'raid detection join raid',
    '/fun-commands': 'toggle commands enable disable per-command',
    '/starboard': 'star board highlights',
    '/suggestions': 'suggestion box',
    '/welcome': 'welcome message autorole welcome card',
    '/birthdays': 'birthday announcements',
    '/counting': 'counting game high score',
    '/reactionroles': 'reaction role menu',
    '/stickyroles': 'sticky roles rejoin',
    '/polls': 'poll button poll',
    '/commands': 'custom commands',
    '/scheduled': 'reminders tasks nickname revert',
    '/tickets': 'support tickets',
    '/modmail': 'mod mail dm staff',
    '/tempvoice': 'temp voice temporary voice channel hub',
    '/youtube': 'youtube uploads live announce',
    '/talk': 'dashboard talk relay say',
    '/ai': 'ai assistant openai',
    '/logging': 'audit log message log',
    '/feed': 'channel feed mirror',
    '/permissions': 'bot manager roles',
    '/analytics': 'stats charts',
    '/staffstats': 'staff activity',
    '/invites': 'invite tracking',
    '/snapshots': 'server snapshot backup',
    '/search': 'find lookup warnings reports rules tickets polls queue',
  };

  let items = [];
  function buildIndex() {
    items = Array.from(document.querySelectorAll('#sidebar-nav a')).map(a => {
      const path = new URL(a.href, location.origin).pathname;
      const feature = '/' + path.split('/').slice(3).join('/'); // strip /guild/{id}
      return {
        label: a.textContent.trim(),
        href: a.href,
        haystack: (a.textContent.trim() + ' ' + (SYNONYMS[feature] || '')).toLowerCase(),
      };
    });
  }

  let activeIndex = -1;
  function render(matches) {
    list.innerHTML = '';
    empty.hidden = matches.length !== 0;
    matches.forEach((item, i) => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = item.href;
      a.textContent = item.label;
      a.className = i === activeIndex ? 'active' : '';
      li.appendChild(a);
      list.appendChild(li);
    });
  }

  function search() {
    const q = input.value.trim().toLowerCase();
    activeIndex = -1;
    if (!q) { render(items); return; }
    render(items.filter(item => item.haystack.includes(q)));
  }

  function open() {
    buildIndex();
    overlay.hidden = false;
    input.value = '';
    search();
    setTimeout(() => input.focus(), 0);
  }
  function close() { overlay.hidden = true; }

  trigger.addEventListener('click', open);
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); overlay.hidden ? open() : close(); }
    else if (e.key === '/' && overlay.hidden && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') { e.preventDefault(); open(); }
  });
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  input.addEventListener('input', search);
  input.addEventListener('keydown', (e) => {
    const links = Array.from(list.querySelectorAll('a'));
    if (e.key === 'Escape') { close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); activeIndex = Math.min(activeIndex + 1, links.length - 1); render(items.filter(it => it.haystack.includes(input.value.trim().toLowerCase()))); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); activeIndex = Math.max(activeIndex - 1, 0); render(items.filter(it => it.haystack.includes(input.value.trim().toLowerCase()))); }
    else if (e.key === 'Enter') { const target = links[activeIndex] || links[0]; if (target) { location.href = target.href; } }
  });
})();
