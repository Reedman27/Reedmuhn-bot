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
