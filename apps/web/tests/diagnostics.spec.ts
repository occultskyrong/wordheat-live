import { expect, test } from '@playwright/test';

test('诊断未知证据和隔离回放确认', async ({ page }) => {
  await page.goto('/replay');
  await expect(page.getByText('尚无真实平台证据')).toBeVisible();
  await page.getByRole('button', { name: '创建隔离回放' }).click();
  await expect(page.getByRole('dialog')).toContainText('不会向正式任务、榜单或主展示投递');
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('复盘导出先显示脱敏确认', async ({ page }) => {
  await page.goto('/analytics/session_demo');
  await page.getByRole('button', { name: '导出脱敏 CSV' }).click();
  await expect(page.getByRole('dialog')).toContainText('不含私有答案、原始 UID 或平台 secret');
});
