import { expect, test } from '@playwright/test';

test('主播台谜底遮罩与暂停恢复', async ({ page }) => {
  await page.goto('/studio/session_demo');
  await expect(page.getByRole('heading', { name: '主播控制台' })).toBeVisible();
  await expect(page.getByText('柠檬')).toHaveCount(0);
  await page.getByRole('button', { name: '查看谜底' }).click();
  await expect(page.getByText('柠檬')).toBeVisible();
  await page.getByRole('button', { name: '重新遮罩' }).click();
  await page.getByRole('button', { name: '暂停', exact: true }).click();
  await expect(page.getByText('回合已暂停 · 合成演练')).toBeVisible();
  await page.getByRole('button', { name: '恢复', exact: true }).click();
  await expect(page.getByRole('button', { name: '暂停', exact: true })).toBeEnabled();
});

test('危险操作确认框支持纯键盘、焦点约束和 Escape 恢复', async ({ page }) => {
  await page.goto('/studio/session_demo');
  const skip = page.getByRole('button', { name: '跳题', exact: true });
  await skip.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('button', { name: '取消', exact: true })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: '确认跳过本轮' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: '关闭', exact: true })).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText(/正在收尾，输入已关闭/)).toBeVisible();
  await expect(skip).toBeDisabled();
  const end = page.getByRole('button', { name: '结束场次', exact: true });
  await end.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(end).toBeFocused();
});
