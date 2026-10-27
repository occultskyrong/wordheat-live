import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { analyticsFixture } from '../../api/fixture/data';
import { AnalyticsPage } from './AnalyticsPage';

describe('场次复盘', () => {
  it('显示可信来源、统计时点和未终态任务', () => {
    render(<AnalyticsPage initialData={analyticsFixture} sessionId="session_demo" />);
    expect(screen.getByText('本地演练 · 合成数据')).toBeInTheDocument();
    expect(screen.getByText(/统计时点：2026-10-25 19:41:08/)).toBeInTheDocument();
    expect(screen.getByText('暂计，待收尾')).toBeInTheDocument();
    expect(screen.getByText('420 ms')).toBeInTheDocument();
    expect(screen.queryByText('观看人数')).not.toBeInTheDocument();
  });

  it('导出前展示字段白名单、来源模式与脱敏确认', async () => {
    const user = userEvent.setup();
    render(<AnalyticsPage initialData={analyticsFixture} sessionId="session_demo" />);
    await user.click(screen.getByRole('button', { name: '导出脱敏 CSV' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('local · 合成演练');
    expect(dialog).toHaveTextContent('round_id、reason、duration、hints、status');
    expect(dialog).toHaveTextContent('不含私有答案、原始 UID 或平台 secret');
  });

  it('过期统计保留内容但禁用导出', () => {
    render(<AnalyticsPage initialData={analyticsFixture} sessionId="session_demo" stale />);
    expect(screen.getByRole('button', { name: '导出脱敏 CSV' })).toBeDisabled();
    expect(screen.getByText(/统计数据已过期/)).toBeInTheDocument();
    expect(screen.getByText('420 ms')).toBeInTheDocument();
  });
});
