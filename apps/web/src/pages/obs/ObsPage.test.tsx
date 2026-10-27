import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { displayFixture } from '../../api/fixture/data';
import type { DisplayView } from '../../api/fixture/types';
import { ObsPage } from './ObsPage';

describe('OBS 公开页面', () => {
  it('显示参与规则和公开反馈，但不显示私有谜底或管理操作', () => {
    render(<ObsPage initialData={displayFixture} />);
    expect(screen.getByText(/发送「猜 A7 苹果」/)).toBeInTheDocument();
    expect(screen.getAllByText('82.4')).toHaveLength(2);
    expect(screen.queryByText('柠檬')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '暂停' })).not.toBeInTheDocument();
  });

  it('重连时明确时间继续而不是暂停', () => {
    render(<ObsPage initialData={{ ...displayFixture, transport: 'RECONNECTING' }} />);
    expect(screen.getByText(/数据重连中，回合时间继续/)).toBeInTheDocument();
    expect(screen.queryByText('已暂停')).not.toBeInTheDocument();
  });

  it('空轮和空列表不沿用旧短码并给出下一步', () => {
    const empty = {
      ...displayFixture,
      round: null,
      latest: null,
      hints: [],
      wordRanking: [],
      playerRanking: [],
      reveal: null,
    } as DisplayView;
    render(<ObsPage initialData={empty} />);
    expect(screen.getByText('等待本轮开始')).toBeInTheDocument();
    expect(screen.getByText('暂无公开提示')).toBeInTheDocument();
    expect(screen.getByText('暂无有效猜词，发送猜词参与本轮')).toBeInTheDocument();
    expect(screen.getByText('暂无场次积分')).toBeInTheDocument();
    expect(screen.queryByText(/A7/)).not.toBeInTheDocument();
  });

  it('PREPARING 和 DRAINING 不继续邀请输入', () => {
    for (const status of ['PREPARING', 'DRAINING'] as const) {
      const data = { ...displayFixture, round: { ...displayFixture.round!, status } };
      const view = render(<ObsPage initialData={data} />);
      expect(screen.queryByText(/发送「猜/)).not.toBeInTheDocument();
      expect(screen.getByText(status === 'PREPARING' ? '本轮准备中' : '正在收尾')).toBeInTheDocument();
      view.unmount();
    }
  });

  it('授权失效清空旧轮和公开结果', () => {
    const data = { ...displayFixture, authorization: 'UNAUTHORIZED' } as DisplayView;
    render(<ObsPage initialData={data} />);
    expect(screen.getByText('展示授权已失效')).toBeInTheDocument();
    expect(screen.queryByText(/A7/)).not.toBeInTheDocument();
    expect(screen.queryByText('橘子')).not.toBeInTheDocument();
  });

  it('不可恢复故障显示清楚故障遮罩', () => {
    render(<ObsPage initialData={{ ...displayFixture, transport: 'FAULTED' }} />);
    expect(screen.getByText('展示连接故障')).toBeInTheDocument();
    expect(screen.queryByText(/发送「猜/)).not.toBeInTheDocument();
  });

  it('按运行用途和场次模式显示公开身份', () => {
    const { rerender } = render(<ObsPage initialData={{ ...displayFixture, runPurpose: 'production', mode: 'official' }} />);
    expect(screen.getByText('正式直播 · 合成数据')).toBeInTheDocument();
    rerender(<ObsPage initialData={{ ...displayFixture, runPurpose: 'test_live', mode: 'replay' }} />);
    expect(screen.getByText('直播测试 · 回放演练 · 合成数据')).toBeInTheDocument();
  });

  it('契约版本错误使用专用故障文案并停止展示旧轮', () => {
    render(<ObsPage initialData={displayFixture} syncError="CONTRACT_VERSION_MISMATCH" />);
    expect(screen.getByText('展示版本不匹配')).toBeInTheDocument();
    expect(screen.getByText('请更新或重新打开展示页面')).toBeInTheDocument();
    expect(screen.queryByText('展示数据需要重新同步')).not.toBeInTheDocument();
    expect(screen.queryByText(/发送「猜/)).not.toBeInTheDocument();
  });

  it('INTERMISSION 显示下一轮计划但不自行换短码', () => {
    const data = {
      ...displayFixture,
      round: { ...displayFixture.round!, status: 'INTERMISSION' as const, nextStartAt: '2026-10-25T12:03:00.000Z' },
    };
    render(<ObsPage initialData={data} />);
    expect(screen.getByText(/等待下一轮确认/)).toBeInTheDocument();
    expect(screen.getByText(/计划开始：/)).toBeInTheDocument();
    expect(screen.queryByText(/发送「猜/)).not.toBeInTheDocument();
  });
});
