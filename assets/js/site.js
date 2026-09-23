(function(){
  const language = document.documentElement.lang === 'en' ? 'en' : 'ru';
  const prefix = language === 'en' ? '../' : '';
  const localized = (row, key) => row[`${key}_${language}`] || row[key] || row[`${key}_ru`] || row[`${key}_en`] || '';
  const escape = value => String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const externalURL = value => {
    try {
      const url = new URL(value);
      return /^(https?:)$/.test(url.protocol) && !url.username && !url.password ? url.href : '';
    } catch(e) { return ''; }
  };
  const imageURL = value => {
    if(typeof value !== 'string') return '';
    if(/^assets\/[a-zA-Z0-9_./-]+$/.test(value) && !value.split('/').includes('..')) return prefix + value;
    return externalURL(value);
  };
  async function loadJSON(paths, accept){
    for(const path of paths){
      try {
        const response = await fetch(prefix + path, {cache: 'no-store'});
        if(!response.ok) continue;
        const value = await response.json();
        if(accept(value)) return value;
      } catch(e) { /* Keep the published HTML when fresh data is unavailable. */ }
    }
    return null;
  }
  const records = value => Array.isArray(value) ? value : value && (value.records || value.items);
  const validRecords = value => Array.isArray(records(value)) && records(value).length > 0 && records(value).every(row => row && typeof row === 'object');

  async function renderIT(){
    const container = document.querySelector('[data-it-list]');
    const counters = document.querySelectorAll('[data-it-count]');
    if(!container && !counters.length) return;
    const catalog = await loadJSON(['data/it/resources.json'], value => validRecords(value) && records(value).every(row => row.id && externalURL(row.url) && imageURL(row.thumb)));
    if(!catalog) return;
    counters.forEach(counter => { counter.textContent = records(catalog).length; });
    if(!container) return;
    container.innerHTML = records(catalog).map(row => {
      const title = localized(row, 'title');
      const kind = localized(row, 'kind');
      const details = [row.language, (row.updated_at || '').slice(0, 10)].filter(Boolean).join(' · ');
      return `<article class="resource-card" data-resource-id="${escape(row.id)}"><img class="resource-image" src="${escape(imageURL(row.thumb))}" alt="${escape(localized(row, 'image_alt') || title)}" loading="lazy"><div class="resource-body"><div class="resource-head"><img src="${prefix}assets/social/github.svg" alt="" aria-hidden="true"><div><h3>${escape(title)}</h3>${kind ? `<div class="kind">${escape(kind)}</div>` : ''}</div></div><p>${escape(localized(row, 'description'))}</p><div class="chips">${(Array.isArray(row.tags) ? row.tags : []).map(tag => `<span class="chip">${escape(tag)}</span>`).join('')}</div>${details ? `<p class="small muted">${escape(details)}</p>` : ''}<a class="repo-link" href="${escape(externalURL(row.url))}" target="_blank" rel="noopener">${row.url.startsWith('https://github.com/') ? 'GitHub' : language === 'en' ? 'Open project' : 'Открыть проект'}</a></div></article>`;
    }).join('');
    container.dataset.loaded = 'true';
  }

  async function renderPublications(){
    const container = document.querySelector('[data-filter-group="pubs"]');
    if(!container) return;
    const payload = await loadJSON(['data/public/publications.json'], validRecords);
    if(!payload) return;
    const rows = records(payload);
    const sourcesFor = row => Array.isArray(row.sources) ? row.sources : [row.provider || row.source || ''].filter(Boolean);
    container.innerHTML = rows.map(row => {
      const sources = sourcesFor(row);
      const title = localized(row, 'title_display') || localized(row, 'title');
      const authors = row.authors_raw || row.authors || '';
      const citation = (language === 'en' ? row.apa_en : row.gost_ru) || [Array.isArray(authors) ? authors.join(', ') : authors, title, row.venue || row.journal || row.metadata_raw, row.year].filter(Boolean).join('. ');
      const url = externalURL(row.url) || (row.doi ? externalURL('https://doi.org/' + row.doi) : '');
      const citations = row.rinc_citations ?? row.citations_risc;
      const typeBadge = row.publication_type === 'preprint' ? `<span class="badge light publication-type">${language === 'en' ? 'Preprint' : 'Препринт'}</span>` : '';
      const badges = typeBadge + sources.map(source => `<span class="badge">${escape(source)}</span>`).join('') + (Number.isFinite(citations) && citations > 0 ? `<span class="badge light">${language === 'en' ? 'RSCI citations' : 'РИНЦ цит.'}: ${citations}</span>` : '');
      const search = [title, row.title, authors, row.venue, row.journal, row.metadata_raw, row.source, row.doi, row.year, row.gost_ru, row.apa_en].filter(Boolean).join(' ');
      return `<article class="pub-row" data-publication-id="${escape(row.id || row.doi || row.elibrary_item_id || '')}" data-search="${escape(search)}" data-year="${escape(row.year || '')}" data-source="${escape(sources.join(' '))}"><div class="pub-year">${escape(row.year || '')}</div><div class="pub-main"><div class="pub-citation">${url ? `<a href="${escape(url)}" target="_blank" rel="noopener">${escape(citation)}</a>` : escape(citation)}</div><div class="meta">${escape(sources.join(' · '))}</div></div><aside class="pub-quality"><div class="metric-badges">${badges}</div><button class="copy-citation" type="button" data-copy="${escape(citation)}" data-done="${language === 'en' ? 'Copied' : 'Скопировано'}">${language === 'en' ? 'Copy APA' : 'Копировать ГОСТ'}</button></aside></article>`;
    }).join('');
    const options = (selector, values, label) => {
      const select = document.querySelector(selector);
      if(!select) return;
      const previous = select.value;
      select.innerHTML = `<option value="">${label}</option>` + values.map(value => `<option value="${escape(value)}">${escape(value)}</option>`).join('');
      if(values.map(String).includes(previous)) select.value = previous;
    };
    options('[data-filter-year="pubs"]', [...new Set(rows.map(row => row.year).filter(Boolean))].sort((a,b) => b-a), language === 'en' ? 'All years' : 'Все годы');
    options('[data-filter-source="pubs"]', [...new Set(rows.flatMap(sourcesFor))].sort(), language === 'en' ? 'All sources' : 'Все источники');
    const counter = document.querySelector('[data-filter-count="pubs"]');
    if(counter && counter.nextSibling) counter.nextSibling.textContent = ' / ' + rows.length;
    applyFilter('pubs');
    container.dataset.loaded = 'true';
  }

  async function renderMedia(){
    const container = document.querySelector('[data-media-list]');
    if(!container) return;
    const payload = await loadJSON(['data/media/published.json', 'data/media/news_mentions.json', 'data/media/published-fallback.json'], validRecords);
    if(!payload) return;
    container.innerHTML = records(payload).map(row => {
      const url = externalURL(row.url);
      const image = imageURL(row.image || row.image_url || row.thumb);
      const title = localized(row, 'title');
      const source = localized(row, 'source_name');
      const pending = language === 'en' && (row.translation_state?.status === 'pending' || !row.title_en || !row.description_en);
      return `<article class="media-card${image ? '' : ' no-image'}" data-media-id="${escape(row.id || '')}"${pending ? ' data-translation-status="pending"' : ''}>${image ? `<a class="media-image" href="${escape(url)}" target="_blank" rel="noopener"><img src="${escape(image)}" alt="" loading="lazy"></a>` : ''}<div class="media-body"><div class="media-meta">${escape([source, (row.published_at || row.date || '').slice(0, 10)].filter(Boolean).join(' · '))}</div><h2><a href="${escape(url)}" target="_blank" rel="noopener">${escape(title)}</a></h2><p>${escape(localized(row, 'description'))}</p>${pending ? '<p class="media-translation-note small muted">English translation pending; original text is shown.</p>' : ''}<a class="inline-link media-link" href="${escape(url)}" target="_blank" rel="noopener">${language === 'en' ? 'Read article' : 'Открыть материал'}</a></div></article>`;
    }).join('');
    container.dataset.loaded = 'true';
  }

  async function renderMetrics(){
    const badges = document.querySelectorAll('.source-metric[data-source]');
    const table = document.querySelector('[data-metrics-table]');
    if(!badges.length && !table) return;
    const metrics = await loadJSON(['data/public/metrics.json'], value => value && ['wos', 'scopus', 'risc'].some(key => value[key]));
    if(!metrics) return;
    const keys = ['publications', 'citations', 'h_index'];
    const applyValues = (elements, source, core = false) => {
      keys.forEach((key, index) => {
        const value = source?.[(core ? 'core_' : '') + key];
        if(elements[index] && Number.isFinite(value) && value >= 0) elements[index].textContent = value;
      });
    };
    badges.forEach(badge => applyValues(badge.querySelectorAll('b'), metrics[({WOS:'wos',SCOPUS:'scopus',RSCI:'risc'})[badge.dataset.source]]));
    if(table){
      table.querySelectorAll('tbody tr').forEach((row, index) => applyValues([...row.querySelectorAll('td')].slice(1), metrics[['wos','scopus','risc','risc'][index]], index === 3));
      table.dataset.loaded = 'true';
    }
    const chart = document.querySelector('.annual-chart');
    const annual = metrics.annual;
    if(chart && Array.isArray(annual?.years) && Array.isArray(annual.risc_publications)){
      const max = Math.max(1, ...annual.risc_publications.filter(Number.isFinite));
      chart.innerHTML = annual.years.map((year, index) => {
        const count = Number.isFinite(annual.risc_publications[index]) ? annual.risc_publications[index] : 0;
        const citations = annual.risc_citations?.[index];
        return `<div class="annual-row"><span>${escape(year)}</span><div class="bar"><span style="width:${Math.max(0, Math.min(100, count/max*100))}%"></span></div><b>${count}</b><em>${Number.isFinite(citations) ? citations + (language === 'en' ? ' cit.' : ' цит.') : ''}</em></div>`;
      }).join('');
    }
    const updated = document.querySelector('[data-elibrary-updated]');
    if(updated && metrics.metadata?.elibrary_updated) updated.textContent = metrics.metadata.elibrary_updated;
  }
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

  Promise.allSettled([renderIT(), renderPublications(), renderMedia(), renderMetrics()]).then(() => {
    document.documentElement.dataset.siteReady = 'true';
  });
})();
