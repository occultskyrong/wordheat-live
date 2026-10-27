import { expect, test } from '@playwright/test';

const routes = [
  ['/setup', '开播准备'],
  ['/studio/session_demo', '主播控制台'],
  ['/words', '题库管理'],
  ['/scoring', '评分实验室'],
  ['/replay', '运行诊断与回放'],
  ['/analytics/session_demo', '场次复盘'],
  ['/settings', '设置与展示预览'],
] as const;

test('七个后台页面可导航且无运行时异常', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  for (const [route, heading] of routes) {
    await page.goto(route);
    await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
    await expect(page.getByText(/全部页面使用合成 fixture/)).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test('后台共享空数据、部分失败和权限异常场景', async ({ page }) => {
  await page.goto('/words?scenario=empty');
  await expect(page.getByText('当前筛选下暂无数据，请返回开播准备或调整筛选')).toBeVisible();
  await page.goto('/words?scenario=partial-error');
  await expect(page.getByRole('alert')).toContainText('部分数据更新失败');
  await expect(page.getByRole('heading', { name: '题库管理', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /Q-018 · 柠檬/ })).toBeVisible();
  await expect(page.getByRole('button', { name: '新建草稿' })).toBeDisabled();
  await expect(page.getByRole('button', { name: '选择 CSV' })).toBeDisabled();
  await page.goto('/analytics/session_demo?scenario=partial-error');
  await expect(page.getByRole('button', { name: '导出脱敏 CSV' })).toBeDisabled();
  await page.goto('/words?scenario=forbidden');
  await expect(page.getByRole('alert')).toContainText('当前身份无权访问此视图');
  await page.goto('/words?scenario=contract-error');
  await expect(page.getByRole('alert')).toContainText('前后端契约版本不匹配');
});

test('根路径进入开播准备，不自动开始场次', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '开播准备' })).toBeVisible();
  await expect(page.getByRole('button', { name: '开始本地演练' })).toBeDisabled();
});
