import { expect, test } from '@playwright/test';

test.use({ viewport: { width: 390, height: 844 } });

test('移动主播台采用双列指标并尽早露出回合操作', async ({ page }) => {
  await page.goto('/studio/session_demo');
  const metrics = page.locator('.studio-metrics');
  await expect(metrics).toHaveCSS('grid-template-columns', /.+ .+/);
  const cards = metrics.locator('.metric-card');
  const first = await cards.nth(0).boundingBox();
  const second = await cards.nth(1).boundingBox();
  const third = await cards.nth(2).boundingBox();
  expect(first).not.toBeNull();
  expect(second).not.toBeNull();
  expect(third).not.toBeNull();
  expect(second!.y).toBeCloseTo(first!.y, 0);
  expect(third!.y).toBeGreaterThan(first!.y);
  const operations = await page.getByRole('heading', { name: '回合操作' }).boundingBox();
  expect(operations!.y).toBeLessThan(700);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('移动题库筛选紧凑排列且只读态与编辑态可区分', async ({ page }) => {
  await page.goto('/words');
  const filters = page.locator('.word-filters');
  await expect(filters).toHaveCSS('grid-template-columns', /.+ .+/);
  const search = await page.getByLabel('搜索题目').boundingBox();
  const category = await page.getByRole('combobox', { name: '分类', exact: true }).boundingBox();
  const difficulty = await page.getByRole('combobox', { name: '难度', exact: true }).boundingBox();
  expect(search!.width).toBeGreaterThan(category!.width * 1.8);
  expect(difficulty!.y).toBeCloseTo(category!.y, 0);
  const firstWord = await page.getByRole('button', { name: /Q-018/ }).boundingBox();
  expect(firstWord!.y).toBeLessThan(700);

  await page.getByRole('button', { name: /Q-018/ }).click();
  const readOnlyBackground = await page.getByLabel('答案', { exact: true }).evaluate((element) => getComputedStyle(element).backgroundColor);
  await page.getByRole('button', { name: /Q-031/ }).click();
  const editableBackground = await page.getByLabel('答案', { exact: true }).evaluate((element) => getComputedStyle(element).backgroundColor);
  expect(readOnlyBackground).not.toBe(editableBackground);
});

test('移动导航显示真实滚动边界且所有入口保持可达', async ({ page }) => {
  await page.goto('/setup');
  const nav = page.getByRole('navigation', { name: '后台页面' });
  const frame = page.locator('.nav-scroll');
  await expect(frame).toHaveAttribute('data-overflow-left', 'false');
  await expect(frame).toHaveAttribute('data-overflow-right', 'true');
  await nav.evaluate((element) => { element.scrollLeft = element.scrollWidth; });
  await expect(frame).toHaveAttribute('data-overflow-left', 'true');
  await expect(frame).toHaveAttribute('data-overflow-right', 'false');
  await page.getByRole('link', { name: '设置', exact: true }).click();
  await expect(page.getByRole('heading', { name: '设置与展示预览', exact: true })).toBeVisible();
});

test('移动复盘使用双列指标并尽早露出趋势', async ({ page }) => {
  await page.goto('/analytics/session_demo');
  const metrics = page.locator('.analytics-metrics');
  await expect(metrics).toHaveCSS('grid-template-columns', /.+ .+/);
  const cards = metrics.locator('.metric-card');
  const first = await cards.nth(0).boundingBox();
  const second = await cards.nth(1).boundingBox();
  const third = await cards.nth(2).boundingBox();
  expect(second!.y).toBeCloseTo(first!.y, 0);
  expect(third!.y).toBeGreaterThan(first!.y);
  const trend = await page.getByRole('heading', { name: '参与趋势' }).boundingBox();
  expect(trend!.y).toBeLessThan(844);
});
