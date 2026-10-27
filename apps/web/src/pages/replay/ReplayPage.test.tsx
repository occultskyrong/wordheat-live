import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { DemoResult } from '../../api/fixture/types';
import { diagnosticsFixture } from '../../api/fixture/data';
import { ReplayPage } from './ReplayPage';

describe('运行诊断与隔离回放', () => {
  it('未知平台履约保持未知，不补绿勾', () => {
    render(<ReplayPage initialData={diagnosticsFixture} />);
    expect(screen.getByText('平台履约')).toBeInTheDocument();
    expect(screen.getByText('尚无真实平台证据')).toBeInTheDocument();
    expect(screen.getByText('unknown')).toBeInTheDocument();
    expect(screen.getByText(/本系统未收到，无法判断上游遗漏/)).toBeInTheDocument();
  });

  it('创建回放前确认来源许可和正式数据隔离', async () => {
    const user = userEvent.setup();
    render(<ReplayPage initialData={diagnosticsFixture} />);
    expect(screen.getByText(/目标 mode：replay/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '创建隔离回放' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('不会向正式任务、榜单或主展示投递');
    expect(screen.getByRole('button', { name: '确认创建回放' })).toBeInTheDocument();
  });

  it('session、reason、cursor 受控且 reason 真正过滤消息', async () => {
    const user = userEvent.setup();
    render(<ReplayPage initialData={diagnosticsFixture} />);
    expect(screen.getByLabelText('session')).toHaveValue('session_demo');
    await user.type(screen.getByLabelText('cursor'), 'cursor_1024');
    await user.selectOptions(screen.getByLabelText('reason'), 'DUPLICATE');
    expect(screen.getByRole('button', { name: 'msg_demo_003' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'msg_demo_001' })).not.toBeInTheDocument();
  });

  it('回放命令按 job 状态启用，取消需要二次确认', async () => {
    const user = userEvent.setup();
    const onCommand = vi.fn(async (command: string): Promise<DemoResult> => ({ status: 'applied', message: command, synthetic: true }));
    render(<ReplayPage initialData={diagnosticsFixture} onCommand={onCommand} />);
    expect(screen.getByRole('button', { name: '开始回放' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '暂停' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '恢复' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '恢复' }));
    expect(await screen.findByText(/replay_demo_01 · running/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '暂停' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '取消回放' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('取消后等待权威任务终态');
    await user.click(screen.getByRole('button', { name: '确认取消回放' }));
    expect(onCommand).toHaveBeenLastCalledWith('replay.cancel');
    expect(await screen.findByText(/replay_demo_01 · canceled/)).toBeInTheDocument();
  });

  it('命令未应用时保留任务状态', async () => {
    const user = userEvent.setup();
    const onCommand = vi.fn(async (): Promise<DemoResult> => ({ status: 'unavailable', message: '版本冲突', synthetic: true }));
    render(<ReplayPage initialData={diagnosticsFixture} onCommand={onCommand} />);
    await user.click(screen.getByRole('button', { name: '恢复' }));
    expect(await screen.findByText(/版本冲突/)).toBeInTheDocument();
    expect(screen.getByText(/replay_demo_01 · paused/)).toBeInTheDocument();
  });
});
