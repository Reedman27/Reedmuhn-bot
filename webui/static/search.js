// Sidebar/page search.
//
// Externalized from an inline <script> block (blocked by this app's
// strict CSP - see confirm.js for the full explanation).
(() => {
  const input = document.getElementById('q');
  const card = document.getElementById('page-results-card');
  const list = document.getElementById('page-results');
  const empty = document.getElementById('page-results-empty');
  if (!input || !card) return;

  // Extra search terms for pages whose feature name differs from its nav
  // label/icon (so e.g. typing "xp" finds the Leveling page). Built from the
  // sidebar's own links, so a new page automatically becomes searchable
  // without touching this file.
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
  };

  const items = Array.from(document.querySelectorAll('#sidebar-nav a')).map(a => {
    const path = new URL(a.href, location.origin).pathname;
    const feature = '/' + path.split('/').slice(3).join('/'); // strip /guild/{id}
    return {
      label: a.textContent.trim(),
      href: a.href,
      haystack: (a.textContent.trim() + ' ' + (SYNONYMS[feature] || '')).toLowerCase(),
    };
  });

  function render() {
    const q = input.value.trim().toLowerCase();
    if (!q) { card.hidden = true; return; }
    const matches = items.filter(item => item.haystack.includes(q));
    card.hidden = false;
    empty.hidden = matches.length !== 0;
    list.innerHTML = '';
    matches.forEach(item => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = item.href;
      a.textContent = item.label;
      li.appendChild(a);
      list.appendChild(li);
    });
  }

  input.addEventListener('input', render);
  render();
})();