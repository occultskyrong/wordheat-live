import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { readinessFixture } from '../../api/fixture/data';
import type { DemoResult } from '../../api/fixture/types';
import { SetupPage } from './SetupPage';

const applied = async (command: string): Promise<DemoResult> => ({ status: 'applied', message: `已应用：${command}`, synthetic: true });

describe('开播准备', () => {
  it('创建草稿和开始演练是两个独立步骤', async () => {
    const user = userEvent.setup();
    const command = vi.fn(applied);
    render(<SetupPage initialData={readinessFixture} onCommand={command} />);

    expect(screen.getByRole('button', { name: '开始本地演练' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '创建场次草稿' }));
    expect(await screen.findByText(/草稿已创建/)).toBeInTheDocument();
    expect(command).toHaveBeenCalledWith('session.create');

    await user.click(screen.getByRole('button', { name: '开始本地演练' }));
    expect(await screen.findByText(/演练已启动/)).toBeInTheDocument();
    expect(command).toHaveBeenLastCalledWith('session.start');
  });

  it('命令等待期间禁止重复创建', async () => {
    const user = userEvent.setup();
    let release!: (result: DemoResult) => void;
    const command = vi.fn(() => new Promise<DemoResult>((resolve) => { release = resolve; }));
    render(<SetupPage initialData={readinessFixture} onCommand={command} />);
    const create = screen.getByRole('button', { name: '创建场次草稿' });
    await user.click(create);
    expect(screen.getByRole('button', { name: '创建中…' })).toBeDisabled();
    expect(command).toHaveBeenCalledTimes(1);
    release({ status: 'applied', message: '已创建', synthetic: true });
    expect(await screen.findByText(/草稿已创建/)).toBeInTheDocument();
  });

  it('布局预览打开独立 OBS preview 页面', () => {
    render(<SetupPage initialData={readinessFixture} />);
    const preview = screen.getByRole('link', { name: '打开布局预览' });
    expect(preview).toHaveAttribute('href', '/obs.html');
    expect(preview).toHaveAttribute('target', '_blank');
    expect(preview).toHaveAttribute('rel', expect.stringContaining('noreferrer'));
  });

  it('真实平台能力保持未接入，不用合成检查冒充放行', async () => {
    const user = userEvent.setup();
    render(<SetupPage initialData={readinessFixture} />);
    expect(screen.getByText(/平台测试 · 待真实 API 接入/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '申请平台测试' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '申请平台测试' }));
    expect(await screen.findByText('待真实 API 接入')).toBeInTheDocument();
    expect(screen.getByText('G2 真实直播')).toBeInTheDocument();
  });
});
