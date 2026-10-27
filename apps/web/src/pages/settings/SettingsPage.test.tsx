import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { settingsFixture } from '../../api/fixture/data';
import { SettingsPage } from './SettingsPage';

describe('设置与展示预览', () => {
  it('保存下一轮规则后当前轮冻结规则保持不变', async () => {
    const user = userEvent.setup();
    render(<SettingsPage initialData={settingsFixture} />);
    const duration = screen.getByLabelText('回合时长（秒）');
    await user.clear(duration);
    await user.type(duration, '300');
    await user.click(screen.getByRole('button', { name: '保存为下一轮规则' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('当前轮继续使用 240 秒');
    await user.click(screen.getByRole('button', { name: '确认保存下一轮规则' }));
    expect(await screen.findByText('下一轮规则已保存 · 合成演练')).toBeInTheDocument();
    expect(screen.getAllByText(/当前冻结规则：/)[0]).toHaveTextContent('当前冻结规则：240 秒');
    expect(screen.getByText((_, element) => element?.tagName === 'P' && Boolean(element.textContent?.includes('下一轮规则：300 秒')))).toBeInTheDocument();
  });

  it('版本冲突保留编辑草稿并提示重新读取', async () => {
    const user = userEvent.setup();
    const command = vi.fn(async () => { throw new Error('VERSION_CONFLICT'); });
    render(<SettingsPage initialData={settingsFixture} onCommand={command} />);
    const duration = screen.getByLabelText('回合时长（秒）');
    await user.clear(duration);
    await user.type(duration, '300');
    await user.click(screen.getByRole('button', { name: '保存为下一轮规则' }));
    await user.click(screen.getByRole('button', { name: '确认保存下一轮规则' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('版本冲突，草稿已保留，请重新读取后再保存');
    expect(duration).toHaveValue(300);
  });

  it('预览偏好只保存本机，维护能力不伪造成功', async () => {
    const user = userEvent.setup();
    render(<SettingsPage initialData={settingsFixture} />);
    await user.click(screen.getByRole('button', { name: '保存本机预览偏好' }));
    expect(screen.getByText('本机预览偏好已保存')).toBeInTheDocument();
    expect(screen.getByText(/不会修改后端全局配置/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '请求备份向导' }));
    await user.click(screen.getByRole('button', { name: '确认请求备份向导' }));
    expect(await screen.findByText('待真实 API 接入')).toBeInTheDocument();
  });

  it('布局和字号变更立即保存，恢复默认同样持久化', async () => {
    const user = userEvent.setup();
    render(<SettingsPage initialData={settingsFixture} />);
    await user.selectOptions(screen.getByLabelText('布局'), 'landscape');
    await user.selectOptions(screen.getByLabelText('展示字号'), '1.2');
    expect(localStorage.getItem('wordheat-display-preferences:v0.1.0')).toContain('"layout":"landscape"');
    expect(localStorage.getItem('wordheat-display-preferences:v0.1.0')).toContain('"fontScale":1.2');
    await user.click(screen.getByRole('button', { name: '恢复设计默认' }));
    expect(screen.getByLabelText('布局')).toHaveValue('portrait');
    expect(localStorage.getItem('wordheat-display-preferences:v0.1.0')).toContain('"layout":"portrait"');
  });

  it('撤票与备份维护都需要明确确认', async () => {
    const user = userEvent.setup();
    render(<SettingsPage initialData={settingsFixture} />);
    await user.click(screen.getByRole('button', { name: '撤销票据' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('撤销后现有展示连接将失效');
    await user.click(screen.getByRole('button', { name: '确认撤销票据' }));
    expect(await screen.findByText('待真实 API 接入')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '请求备份向导' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('备份前确认当前没有活动场');
    await user.click(screen.getByRole('button', { name: '确认请求备份向导' }));
    expect(await screen.findAllByText('待真实 API 接入')).toHaveLength(2);
  });

  it('活动场存在时禁止进入备份维护', () => {
    const data = { ...settingsFixture, activeSession: { id: 'session_demo', status: 'RUNNING' } };
    render(<SettingsPage initialData={data} />);
    expect(screen.getByRole('button', { name: '请求备份向导' })).toBeDisabled();
    expect(screen.getByText(/活动场 session_demo · RUNNING/)).toBeInTheDocument();
  });

  it('复制能力明确待接入且不伪报已复制', async () => {
    const user = userEvent.setup();
    render(<SettingsPage initialData={settingsFixture} />);
    await user.click(screen.getByRole('button', { name: '复制一次性预览链接：待真实 API 接入' }));
    expect(screen.getByText('复制一次性预览链接：待真实 API 接入', { selector: '.inline-notice div' })).toBeInTheDocument();
    expect(screen.queryByText(/已复制/)).not.toBeInTheDocument();
  });
});
