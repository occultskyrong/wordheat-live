import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { displayFixture } from '../../api/fixture/data';
import type { RoundView } from '../../api/fixture/types';
import { Clock } from './Clock';

function round(status: RoundView['status']): RoundView {
  return {
    ...displayFixture.round!,
    status,
    clock: {
      timeOrigin: 'local_rehearsal',
      deadlineAt: '2026-10-25T12:01:00.000Z',
      remainingMs: 60000,
    },
  };
}

describe('权威回合计时', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(performance, 'now').mockReturnValue(1000);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('RUNNING 根据 serverTime 与 performance.now 推进', () => {
    const mono = vi.spyOn(performance, 'now').mockReturnValue(1000);
    render(<Clock {...({ round: round('RUNNING'), serverTime: '2026-10-25T12:00:00.000Z' } as any)} />);
    expect(screen.getByText('01:00')).toBeInTheDocument();
    mono.mockReturnValue(2000);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(screen.getByText('00:59')).toBeInTheDocument();
  });

  it('PAUSED 使用 remainingMs 冻结', () => {
    const mono = vi.spyOn(performance, 'now').mockReturnValue(1000);
    render(<Clock {...({ round: round('PAUSED'), serverTime: '2026-10-25T12:00:00.000Z' } as any)} />);
    mono.mockReturnValue(6000);
    act(() => { vi.advanceTimersByTime(5000); });
    expect(screen.getByText('01:00')).toBeInTheDocument();
    expect(screen.getByText(/已暂停/)).toBeInTheDocument();
  });

  it('DRAINING 显示收尾而不是继续邀请输入', () => {
    render(<Clock {...({ round: round('DRAINING'), serverTime: '2026-10-25T12:00:00.000Z' } as any)} />);
    expect(screen.getAllByText(/正在收尾/).length).toBeGreaterThan(0);
  });

  it('INTERMISSION 按 nextStartAt 倒计时，到零后只等待权威确认', () => {
    const mono = vi.spyOn(performance, 'now').mockReturnValue(1000);
    const intermission = { ...round('INTERMISSION'), nextStartAt: '2026-10-25T12:01:00.000Z' };
    render(<Clock round={intermission} serverTime="2026-10-25T12:00:00.000Z" />);
    expect(screen.getByText('01:00')).toBeInTheDocument();
    mono.mockReturnValue(61000);
    act(() => { vi.advanceTimersByTime(60000); });
    expect(screen.getByText('00:00')).toBeInTheDocument();
    expect(screen.getByText(/等待下一轮确认/)).toBeInTheDocument();
  });
});
