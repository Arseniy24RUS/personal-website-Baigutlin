import { expect, test } from '@playwright/test';

for (const language of ['ru', 'en'] as const) {
  test(`IT catalog preserves the six editorial cards in ${language}`, async ({ page, request }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    const catalog = await (await request.get('/data/it/resources.json')).json();
    const baseline = await (await request.get('/data/it/retention_baseline.json')).json();
    await page.goto(language === 'en' ? '/en/it.html' : '/it.html');
    await expect(page.locator('[data-it-list]')).toHaveAttribute('data-loaded', 'true');
    await expect(page).toHaveTitle(language === 'en' ? /IT Resources/ : /ИТ-ресурсы/);
    await expect(page.locator('[data-resource-id]')).toHaveCount(catalog.items.length);
    const rendered = await page.locator('[data-resource-id]').evaluateAll(cards => cards.map(card => card.getAttribute('data-resource-id')));
    expect(rendered.filter(id => baseline.items.some((item: {id: string}) => item.id === id))).toEqual(baseline.items.map((item: {id: string}) => item.id));
    for (const item of baseline.items) {
      const card = page.locator(`[data-resource-id="${item.id}"]`);
      await expect(card.locator('h3')).toHaveText(item[`title_${language}`]);
      await expect(card.locator('.kind')).toHaveText(item[`kind_${language}`]);
      await expect(card.locator('p').first()).toHaveText(item[`description_${language}`]);
      await expect(card.locator('.repo-link')).toHaveAttribute('href', item.url);
      await expect(card.locator('.resource-image')).toHaveAttribute('alt', item[`image_alt_${language}`]);
      await card.scrollIntoViewIfNeeded();
      await expect.poll(() => card.locator('.resource-image').evaluate(img => (img as HTMLImageElement).complete && (img as HTMLImageElement).naturalWidth > 0)).toBe(true);
    }
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(2);
  });
}

test('new discovery renders and language link preserves the destination', async ({ page, request }) => {
  const catalog = await (await request.get('/data/it/resources.json')).json();
  const added = {...catalog.items[0], id: 'new-project', title_ru: 'Новый проект', title_en: 'New project', url: 'https://github.com/Danil-phy-cmp-120/new-project'};
  await page.route('**/data/it/resources.json', route => route.fulfill({contentType: 'application/json', body: JSON.stringify({...catalog, items: [...catalog.items, added]})}));
  await page.goto('/it.html');
  await expect(page.locator('[data-resource-id="new-project"] h3')).toHaveText('Новый проект');
  if (await page.locator('.nav-toggle').isVisible()) await page.locator('.nav-toggle').click();
  await page.locator('.lang-link').click();
  await expect(page).toHaveURL(/\/en\/it\.html$/);
  await expect(page.locator('[data-resource-id="new-project"] h3')).toHaveText('New project');
});

test('invalid IT JSON retains the published six cards', async ({ page }) => {
  await page.route('**/data/it/resources.json', route => route.fulfill({contentType: 'application/json', body: 'invalid json'}));
  await page.goto('/it.html');
  await expect(page.locator('html')).toHaveAttribute('data-site-ready', 'true');
  await expect(page.locator('.resource-card')).toHaveCount(6);
});
