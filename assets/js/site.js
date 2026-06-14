(function(){
  const year = document.querySelector('[data-current-year]');
  if(year) year.textContent = new Date().getFullYear();

  document.querySelectorAll('.top').forEach(header => {
    const toggle = header.querySelector('.nav-toggle');
    const nav = header.querySelector('.nav');
    if(!toggle || !nav) return;
    toggle.addEventListener('click', () => {
      const open = !header.classList.contains('is-open');
      header.classList.toggle('is-open', open);
      toggle.setAttribute('aria-expanded', String(open));
    });
    nav.querySelectorAll('a').forEach(link => {
      link.addEventListener('click', () => {
        header.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
      });
    });
  });

  function applyFilter(target){
    const group = document.querySelector(`[data-filter-group="${target}"]`);
    if(!group) return;
    const input = document.querySelector(`[data-filter-input="${target}"]`);
    const yearSelect = document.querySelector(`[data-filter-year="${target}"]`);
    const sourceSelect = document.querySelector(`[data-filter-source="${target}"]`);
    const counter = document.querySelector(`[data-filter-count="${target}"]`);
    const q = (input && input.value || '').trim().toLowerCase();
    const y = yearSelect && yearSelect.value || '';
    const src = sourceSelect && sourceSelect.value || '';
    let visible = 0;
    group.querySelectorAll('[data-search]').forEach(item => {
      const textHit = !q || (item.getAttribute('data-search') || '').toLowerCase().includes(q);
      const yearHit = !y || item.getAttribute('data-year') === y;
      const srcHit = !src || (item.getAttribute('data-source') || '').includes(src);
      const hit = textHit && yearHit && srcHit;
      item.classList.toggle('hidden', !hit);
      if(hit) visible += 1;
    });
    if(counter) counter.textContent = visible;
  }

  document.querySelectorAll('[data-filter-input]').forEach(input => {
    const target = input.getAttribute('data-filter-input');
    input.addEventListener('input', () => applyFilter(target));
    applyFilter(target);
  });
  document.querySelectorAll('[data-filter-year], [data-filter-source]').forEach(select => {
    const target = select.getAttribute('data-filter-year') || select.getAttribute('data-filter-source');
    select.addEventListener('change', () => applyFilter(target));
    applyFilter(target);
  });

  document.addEventListener('click', async event => {
    const btn = event.target.closest('[data-copy]');
    if(!btn) return;
    const initial = btn.dataset.initial || btn.textContent;
    btn.dataset.initial = initial;
    try {
      await navigator.clipboard.writeText(btn.getAttribute('data-copy') || '');
      btn.textContent = btn.dataset.done || 'Copied';
      setTimeout(() => { btn.textContent = initial; }, 1400);
    } catch(e) {
      btn.textContent = btn.dataset.fail || 'Copy failed';
      setTimeout(() => { btn.textContent = initial; }, 1400);
    }
  });

  const modal = document.getElementById('diploma-modal');
  if(modal){
    const title = document.getElementById('diploma-modal-title');
    const image = document.getElementById('diploma-modal-image');
    const download = document.getElementById('diploma-modal-download');
    const close = () => {
      modal.hidden = true;
      if(image) image.src = '';
    };
    document.querySelectorAll('[data-modal-src]').forEach(btn => {
      btn.addEventListener('click', () => {
        if(title) title.textContent = btn.getAttribute('data-modal-title') || '';
        if(image) image.src = btn.getAttribute('data-modal-src') || '';
        if(download) download.href = btn.getAttribute('data-modal-download') || '#';
        modal.hidden = false;
      });
    });
    modal.addEventListener('click', event => {
      if(event.target === modal || event.target.closest('[data-close-modal]')) close();
    });
    document.addEventListener('keydown', event => {
      if(event.key === 'Escape' && !modal.hidden) close();
    });
  }

  document.documentElement.dataset.siteReady = 'true';
})();
