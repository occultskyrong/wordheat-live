import { expect, test, type Page } from '@playwright/test';

async function setPreviewPreferences(page: Page, layout: 'portrait' | 'landscape', fontScale = 1) {
  await page.addInitScript((preferences) => {
    localStorage.setItem('wordheat-display-preferences:v0.1.0', JSON.stringify(preferences));
  }, { layout, fontScale, safeArea: true, reducedMotion: true });
}

async function expectCanvasToFit(page: Page, width: number, height: number) {
  const canvas = page.locator('.obs-canvas');
  await expect(canvas).toHaveAttribute('data-design', `${width}x${height}`);
  const dimensions = await canvas.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { clientWidth: element.clientWidth, clientHeight: element.clientHeight, scrollWidth: element.scrollWidth, scrollHeight: element.scrollHeight, left: rect.left, top: rect.top, width: rect.width, height: rect.height };
  });
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth);
  expect(dimensions.scrollHeight).toBeLessThanOrEqual(dimensions.clientHeight);
  expect(Math.abs(dimensions.left)).toBeLessThan(1);
  expect(Math.abs(dimensions.top)).toBeLessThan(1);
  expect(Math.abs(dimensions.width - width)).toBeLessThan(1);
  expect(Math.abs(dimensions.height - height)).toBeLessThan(1);
  const panelsOverflow = await page.locator('.obs-rule, .obs-round, .obs-hints, .obs-feedback, .obs-ranking, .obs-players, .obs-reveal').evaluateAll((elements) => elements.filter((element) => element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1).map((element) => element.className));
  expect(panelsOverflow).toEqual([]);
}

async function expectSafeAndReadable(page: Page, scenario: string, fontScale: number, layout: 'portrait' | 'landscape') {
  const safe = await page.getByLabel('预览安全区').boundingBox();
  expect(safe).not.toBeNull();
  for (const selector of ['.obs-rule', '.obs-round', '.obs-feedback', '.obs-ranking', '.obs-players']) {
    const target = page.locator(selector);
    if (await target.count() === 0 || !await target.isVisible()) continue;
    const rect = await target.boundingBox();
    expect(rect, selector).not.toBeNull();
    expect(rect!.x, selector).toBeGreaterThanOrEqual(safe!.x - 1);
    expect(rect!.y, selector).toBeGreaterThanOrEqual(safe!.y - 1);
    expect(rect!.x + rect!.width, selector).toBeLessThanOrEqual(safe!.x + safe!.width + 1);
    expect(rect!.y + rect!.height, selector).toBeLessThanOrEqual(safe!.y + safe!.height + 1);
  }
  if (scenario !== 'revealed') {
    await expect(page.locator('.obs-ranking li')).toHaveCount(8);
    const rows = await page.locator('.obs-ranking li').evaluateAll((elements) => elements.map((element) => {
      const style = getComputedStyle(element);
      return { height: element.getBoundingClientRect().height, lineHeight: parseFloat(style.lineHeight), overflowY: style.overflowY };
    }));
    for (const row of rows) {
      expect(row.height).toBeGreaterThanOrEqual(row.lineHeight - 1);
      expect(row.overflowY).toBe('visible');
    }
  }
  if (scenario === 'normal') {
    const hintSize = parseFloat(await page.locator('.obs-hints span').first().evaluate((element) => getComputedStyle(element).fontSize));
    const rankingSize = parseFloat(await page.locator('.obs-ranking li').first().evaluate((element) => getComputedStyle(element).fontSize));
    const categorySize = parseFloat(await page.locator('.obs-round .obs-section-label').evaluate((element) => getComputedStyle(element).fontSize));
    expect(hintSize).toBeCloseTo((layout === 'landscape' ? 34 : 38) * fontScale, 1);
    expect(rankingSize).toBeCloseTo((layout === 'landscape' ? 32 : 36) * fontScale, 1);
    expect(categorySize).toBeCloseTo((layout === 'landscape' ? 32 : 36) * fontScale, 1);
  }
}

test('公开 OBS 只读且不含私有谜底或后台导航', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/obs/session_demo');
  await expect(page.getByRole('heading', { name: '语义猜词' })).toBeVisible();
  await expect(page.getByText(/发送「猜 A7 苹果」/)).toBeVisible();
  await expect(page.getByRole('navigation')).toHaveCount(0);
  await expect(page.getByText('柠檬')).toHaveCount(0);
  await expect(page.getByRole('button')).toHaveCount(0);
  await expect(page.getByLabel('预览安全区')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('独立 obs.html 入口使用默认竖屏预览与安全区', async ({ page }) => {
  await page.goto('/obs.html');
  await expect(page.locator('.obs-stage')).toHaveAttribute('data-synthetic', 'true');
  await expect(page.locator('.obs-stage')).toHaveAttribute('data-layout', 'portrait');
  await expect(page.getByText('本地演练 · 合成数据')).toBeVisible();
  await expect(page.getByLabel('预览安全区')).toBeVisible();
});

for (const fixture of [
  { layout: 'landscape', width: 1920, height: 1080 },
  { layout: 'portrait', width: 1080, height: 1920 },
] as const) {
  test(`${fixture.layout} 三档字号、长文本和揭晓均保持安全区内完整可读`, async ({ page }) => {
    await page.setViewportSize({ width: fixture.width, height: fixture.height });
    for (const fontScale of [0.9, 1, 1.2]) {
      await setPreviewPreferences(page, fixture.layout, fontScale);
      for (const scenario of ['normal', 'long-content', 'revealed']) {
        await page.goto(`/obs.html?scenario=${scenario}`);
        await expect(page.locator('.obs-stage')).toHaveAttribute('data-reduced-motion', 'true');
        await expect(page.locator('.obs-stage')).toHaveAttribute('data-font-scale', String(fontScale));
        await expectCanvasToFit(page, fixture.width, fixture.height);
        await expectSafeAndReadable(page, scenario, fontScale, fixture.layout);
        const animation = await page.locator('.obs-canvas').evaluate((element) => getComputedStyle(element).animationName);
        expect(animation).toBe('none');
        if (scenario === 'long-content') {
          await expect(page.locator('.obs-hints img')).toHaveCount(0);
          await expect(page.getByText(/<img src=x onerror=alert\(1\)>/).first()).toBeVisible();
        }
        if (scenario === 'revealed') {
          await expect(page.locator('.obs-reveal')).toContainText('杨桃');
          await expect(page.locator('.obs-reveal')).toContainText('TIMEOUT');
          await expect(page.locator('.obs-feedback')).toBeVisible();
        }
      }
    }
  });
}

test('独立 OBS 契约错误显示专用更新提示', async ({ page }) => {
  await page.goto('/obs.html?scenario=contract-error');
  await expect(page.getByRole('alert')).toContainText('展示版本不匹配');
  await expect(page.getByRole('alert')).toContainText('请更新或重新打开展示页面');
  await expect(page.getByText(/发送「猜/)).toHaveCount(0);
  await expect(page.locator('.obs-canvas')).toHaveAttribute('data-revealed', 'false');
});

test('OBS 空轮、重连、故障和撤权给出明确状态并清除不可用内容', async ({ page }) => {
  await page.goto('/obs/session_demo?scenario=empty');
  await expect(page.getByText('等待本轮开始')).toBeVisible();
  await expect(page.getByText('暂无最新反馈')).toBeVisible();
  await expect(page.getByText(/发送「猜/)).toHaveCount(0);
  await page.goto('/obs/session_demo?scenario=partial-error');
  await expect(page.getByText(/数据重连中，回合时间继续/)).toBeVisible();
  await page.goto('/obs/session_demo?scenario=faulted');
  await expect(page.getByRole('alert')).toContainText('展示连接故障');
  await expect(page.getByText(/发送「猜/)).toHaveCount(0);
  await page.goto('/obs/session_demo?scenario=unauthorized');
  await expect(page.getByRole('alert')).toContainText('展示授权已失效');
  await expect(page.getByText('橘子')).toHaveCount(0);
});
