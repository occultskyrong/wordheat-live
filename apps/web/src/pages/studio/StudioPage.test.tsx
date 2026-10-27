import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { studioFixture } from '../../api/fixture/data';
import type { DemoResult } from '../../api/fixture/types';
import { StudioPage } from './StudioPage';

const applied = async (command: string): Promise<DemoResult> => ({ status: 'applied', message: `已应用：${command}`, synthetic: true });

describe('主播控制台', () => {
  it('谜底默认遮罩，明确私有后才显示', async () => {
    const user = userEvent.setup();
    render(<StudioPage initialData={studioFixture} />);
    expect(screen.queryByText('柠檬')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看谜底' }));
    expect(screen.getByText('柠檬')).toBeInTheDocument();
    expect(screen.getByText('私有，勿捕获')).toBeInTheDocument();
  });

  it('暂停成功后等待权威状态，等待中禁止重复命令', async () => {
    const user = userEvent.setup();
    let release!: (result: DemoResult) => void;
    const command = vi.fn(() => new Promise<DemoResult>((resolve) => { release = resolve; }));
    render(<StudioPage initialData={studioFixture} onCommand={command} />);
    await user.click(screen.getByRole('button', { name: '暂停' }));
    expect(screen.getByRole('button', { name: '暂停处理中…' })).toBeDisabled();
    expect(command).toHaveBeenCalledWith('round.pause');
    release({ status: 'applied', message: '已暂停', synthetic: true });
    expect(await screen.findByText('回合已暂停 · 合成演练')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '恢复' })).toBeEnabled();
  });

  it('命令未应用时保留权威状态并显示失败消息', async () => {
    const user = userEvent.setup();
    const command = vi.fn(async (): Promise<DemoResult> => ({ status: 'unavailable', message: '版本冲突，未应用', synthetic: true }));
    render(<StudioPage initialData={studioFixture} onCommand={command} />);
    await user.click(screen.getByRole('button', { name: '暂停' }));
    expect(await screen.findByText('版本冲突，未应用')).toBeInTheDocument();
    expect(screen.getByText('RUNNING')).toBeInTheDocument();
    expect(screen.queryByText('回合已暂停 · 合成演练')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '暂停' })).toBeEnabled();
  });

  it('发布预审提示前展示确切文本，应用后清空待发布提示', async () => {
    const user = userEvent.setup();
    const command = vi.fn(applied);
    render(<StudioPage initialData={studioFixture} onCommand={command} />);
    await user.click(screen.getByRole('button', { name: '发布预审提示' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('果皮常用来做饮品或调味');
    await user.click(screen.getByRole('button', { name: '确认发布预审提示' }));
    expect(command).toHaveBeenCalledWith('round.reveal_hint');
    expect(await screen.findByText('已应用：round.reveal_hint')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发布预审提示' })).toBeDisabled();
  });

  it('重连时禁用依赖旧租约的暂停操作', () => {
    const data = structuredClone(studioFixture);
    data.display.transport = 'RECONNECTING';
    render(<StudioPage initialData={data} />);
    expect(screen.getByRole('button', { name: '暂停' })).toBeDisabled();
    expect(screen.getByText(/重连中，回合计时继续/)).toBeInTheDocument();
  });

  it('跳题必须确认并明确积分冲销', async () => {
    const user = userEvent.setup();
    const command = vi.fn(applied);
    render(<StudioPage initialData={studioFixture} onCommand={command} />);
    await user.click(screen.getByRole('button', { name: '跳题' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('冲销本轮积分');
    await user.click(screen.getByRole('button', { name: '确认跳过本轮' }));
    expect(command).toHaveBeenCalledWith('round.skip');
    expect(await screen.findByText('DRAINING')).toBeInTheDocument();
    expect(screen.getByText(/正在收尾/)).toBeInTheDocument();
  });

  it('空轮只允许开始新轮，不读取旧轮字段', () => {
    const data = structuredClone(studioFixture);
    data.display.round = null;
    render(<StudioPage initialData={data} />);
    expect(screen.getByRole('button', { name: '开始新轮' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '恢复' })).toBeDisabled();
    expect(screen.getByText(/当前轮 尚未开始 · 后端版本 —/)).toBeInTheDocument();
    expect(screen.queryByText(/round_demo/)).not.toBeInTheDocument();
  });

  it('DRAINING 禁止暂停、恢复和新轮并说明收尾', () => {
    const data = structuredClone(studioFixture);
    data.display.round = { ...data.display.round!, status: 'DRAINING' };
    render(<StudioPage initialData={data} />);
    expect(screen.getByRole('button', { name: '暂停' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '恢复' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '开始新轮' })).toBeDisabled();
    expect(screen.getByText(/正在收尾，输入已关闭/)).toBeInTheDocument();
  });

  it('REVEALED 和 INTERMISSION 显示原因并允许下一轮', () => {
    for (const status of ['REVEALED', 'INTERMISSION'] as const) {
      const data = structuredClone(studioFixture);
      data.display.round = { ...data.display.round!, status, reason: '合成回合已结算' };
      const view = render(<StudioPage initialData={data} />);
      expect(screen.getByText(/合成回合已结算/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '开始下一轮' })).toBeEnabled();
      view.unmount();
    }
  });

  it('FAULTED 恢复需要确认且不擅自改为 RUNNING', async () => {
    const user = userEvent.setup();
    const command = vi.fn(applied);
    const data = structuredClone(studioFixture);
    data.display.round = { ...data.display.round!, status: 'FAULTED', reason: '合成推理故障' };
    render(<StudioPage initialData={data} onCommand={command} />);
    await user.click(screen.getByRole('button', { name: '恢复场次' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('恢复后等待权威快照');
    await user.click(screen.getByRole('button', { name: '确认恢复场次' }));
    expect(command).toHaveBeenCalledWith('session.recover');
    expect(await screen.findByText('FAULTED')).toBeInTheDocument();
    expect(screen.queryByText('RUNNING')).not.toBeInTheDocument();
  });

  it('确认命令 pending 时禁止重复提交', async () => {
    const user = userEvent.setup();
    let release!: (result: DemoResult) => void;
    const command = vi.fn(() => new Promise<DemoResult>((resolve) => { release = resolve; }));
    render(<StudioPage initialData={studioFixture} onCommand={command} />);
    await user.click(screen.getByRole('button', { name: '跳题' }));
    await user.click(screen.getByRole('button', { name: '确认跳过本轮' }));
    expect(screen.getByRole('button', { name: '确认处理中…' })).toBeDisabled();
    expect(command).toHaveBeenCalledTimes(1);
    release({ status: 'applied', message: '等待权威收尾', synthetic: true });
    expect(await screen.findByText('等待权威收尾')).toBeInTheDocument();
  });

  it('作废和结束场次都必须确认', async () => {
    const user = userEvent.setup();
    const command = vi.fn(applied);
    render(<StudioPage initialData={studioFixture} onCommand={command} />);
    await user.click(screen.getByRole('button', { name: '作废本轮' }));
    await user.click(screen.getByRole('button', { name: '确认作废本轮' }));
    expect(command).toHaveBeenCalledWith('round.void');
    await user.click(screen.getByRole('button', { name: '结束场次' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('关闭输入并等待收尾');
    await user.click(screen.getByRole('button', { name: '确认结束场次' }));
    expect(command).toHaveBeenCalledWith('session.end');
  });
});
