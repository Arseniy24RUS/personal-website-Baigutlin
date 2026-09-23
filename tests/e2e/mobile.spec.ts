import { expect, test } from '@playwright/test';

const pages = ['/', '/publications.html', '/media.html', '/it.html', '/projects.html', '/diplomas.html', '/metrics.html'];
for (const path of pages) {
  test(`${path} loads without runtime errors or horizontal overflow`, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(path);
    await expect(page.locator('html')).toHaveAttribute('data-site-ready', 'true');
    await expect(page.locator('.brand')).toBeVisible();
    await expect(page.locator('main')).not.toBeEmpty();
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(2);
  });
}

test('publication search, year filter and translated route use current JSON', async ({page, request}) => {
  const records = await (await request.get('/data/public/publications.json')).json();
  await page.goto('/publications.html');
  await expect(page.locator('[data-filter-group="pubs"]')).toHaveAttribute('data-loaded', 'true');
  await expect(page.locator('.pub-row')).toHaveCount(records.length);
  const total = records.length;
  await page.locator('[data-filter-input="pubs"]').fill(records[0].title);
  await expect.poll(() => page.locator('.pub-row:not(.hidden)').count()).toBeGreaterThan(0);
  await expect.poll(() => page.locator('.pub-row:not(.hidden)').count()).toBeLessThan(total);
  await page.locator('[data-filter-input="pubs"]').fill('');
  await page.locator('[data-filter-year="pubs"]').selectOption(String(records[0].year));
  await expect(page.locator('[data-filter-count="pubs"]')).toHaveText(String(records.filter((row: {year: number}) => row.year === records[0].year).length));
  if(await page.locator('.nav-toggle').isVisible()) await page.locator('.nav-toggle').click();
  await page.locator('.lang-link').click();
  await expect(page).toHaveURL(/\/en\/publications\.html$/);
  await expect(page.locator('[data-filter-source="pubs"] option').first()).toHaveText('All sources');
});

test('new publications become searchable without rebuilding HTML', async ({page, request}) => {
  const records = await (await request.get('/data/public/publications.json')).json();
  const row = {id:'new-paper', title:'Новая статья о материалах', title_ru:'Новая статья о материалах', year:2026, doi:'10.1234/new-paper', sources:['OpenAlex'], authors_raw:'Байгутлин Д. Р.', gost_ru:'Байгутлин Д. Р. Новая статья о материалах. 2026.'};
  await page.route('**/data/public/publications.json', route => route.fulfill({contentType:'application/json',body:JSON.stringify([...records,row])}));
  await page.goto('/publications.html');
  await page.locator('[data-filter-input="pubs"]').fill(row.doi);
  await expect(page.locator('.pub-row:not(.hidden)')).toHaveCount(1);
  await expect(page.locator('.pub-row:not(.hidden)')).toContainText(row.title);
  await expect(page.locator('[data-filter-year="pubs"] option[value="2026"]')).toHaveCount(1);
});

for (const language of ['ru', 'en'] as const) {
  test(`explicit preprint type and collected authors render in ${language}`, async ({page}) => {
    const rows = [
      {id: 'typed-preprint', title: 'Thermoelectric screening', year: 2026, publication_type: 'preprint', authors_raw: 'Danil Baigutlin, Maria Matyunina', sources: ['OpenAlex'], url: 'https://arxiv.org/abs/2601.00001'},
      {id: 'journal-paper', title: 'Heusler alloys', year: 2026, publication_type: 'journal-article', authors: ['D. Baigutlin', 'V. Sokolovskiy'], sources: ['Crossref']},
      {id: 'untyped-paper', title: 'Untyped repository record', year: 2026, sources: ['arXiv'], url: 'https://arxiv.org/abs/2601.00002'},
    ];
    await page.route('**/data/public/publications.json', route => route.fulfill({contentType: 'application/json', body: JSON.stringify(rows)}));
    await page.goto(language === 'en' ? '/en/publications.html' : '/publications.html');
    await expect(page.locator('[data-filter-group="pubs"]')).toHaveAttribute('data-loaded', 'true');
    await expect(page.locator('.publication-type')).toHaveCount(1);
    await expect(page.locator('[data-publication-id="typed-preprint"] .publication-type')).toHaveText(language === 'en' ? 'Preprint' : 'Препринт');
    await expect(page.locator('[data-publication-id="typed-preprint"] .pub-citation')).toContainText(rows[0].authors_raw!);
    await expect(page.locator('[data-publication-id="journal-paper"] .pub-citation')).toContainText('D. Baigutlin, V. Sokolovskiy');
    await page.locator('[data-filter-input="pubs"]').fill('Maria Matyunina');
    await expect(page.locator('.pub-row:not(.hidden)')).toHaveCount(1);
    await expect(page.locator('.pub-row:not(.hidden) .publication-type')).toBeVisible();
  });
}

for (const language of ['ru','en'] as const) {
  test(`media preserves editorial content and fallback in ${language}`, async ({page, request}) => {
    const payload = await (await request.get('/data/media/published.json')).json();
    const records = payload.records;
    await page.route('**/data/media/published.json', route => route.fulfill({contentType:'application/json',body:'invalid json'}));
    await page.goto(language === 'en' ? '/en/media.html' : '/media.html');
    await expect(page.locator('[data-media-list]')).toHaveAttribute('data-loaded','true');
    await expect(page.locator('.media-card')).toHaveCount(records.length);
    for(const record of records) {
      const card=page.locator(`[data-media-id="${record.id}"]`);
      await expect(card.locator('h2')).toHaveText(record[`title_${language}`] || record.title_ru || record.title);
      await expect(card.locator('.media-link')).toHaveAttribute('href',record.url);
    }
  });
}

test('pending English media translation displays original text explicitly', async ({page}) => {
  const row={id:'pending',url:'https://example.org/news',title_ru:'Новый материал',description_ru:'Исследование Данила Байгутлина',source_name_ru:'ЧелГУ',translation_state:{status:'pending'}};
  await page.route('**/data/media/published.json', route=>route.fulfill({contentType:'application/json',body:JSON.stringify({records:[row]})}));
  await page.goto('/en/media.html');
  await expect(page.locator('.media-card h2')).toHaveText(row.title_ru);
  await expect(page.locator('.media-translation-note')).toContainText('English translation pending');
});

test('metrics refresh uses data and preserves zero as a valid value', async ({page}) => {
  const metrics={wos:{publications:99,citations:300,h_index:0},scopus:{publications:28,citations:205,h_index:8},risc:{publications:70,citations:240,h_index:9,core_publications:35,core_citations:170,core_h_index:7}};
  await page.route('**/data/public/metrics.json',route=>route.fulfill({contentType:'application/json',body:JSON.stringify(metrics)}));
  await page.goto('/publications.html');
  await expect(page.locator('.source-metric[data-source="WOS"] b')).toHaveText(['99','300','0']);
  await page.goto('/metrics.html');
  await expect(page.locator('[data-metrics-table]')).toHaveAttribute('data-loaded','true');
  await expect(page.locator('[data-metrics-table] tbody tr').first().locator('td')).toHaveText(['Web of Science','99','300','0']);
});
