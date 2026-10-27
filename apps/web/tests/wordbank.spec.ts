import { expect, test } from '@playwright/test';

test('题库筛选、只读版本与 CSV 错误可见', async ({ page }) => {
  await page.goto('/words');
  await page.getByRole('button', { name: /Q-018/ }).click();
  await expect(page.getByLabel('答案', { exact: true })).toHaveAttribute('readonly', '');
  await page.getByLabel('搜索题目').fill('风筝');
  await expect(page.getByRole('button', { name: /Q-031/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /Q-018/ })).toHaveCount(0);
  await expect(page.getByText('第 42 行 · word')).toBeVisible();
});
