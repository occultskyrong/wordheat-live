import { useEffect, useMemo, useState } from 'react';
import type { RoundView } from '../../api/fixture/types';

export function remainingClockMs(round: RoundView, estimatedServerNow: number): number {
  if (round.status === 'INTERMISSION') {
    const nextAt = round.nextStartAt ? Date.parse(round.nextStartAt) : NaN;
    return Number.isFinite(nextAt) ? Math.max(0, nextAt - estimatedServerNow) : 0;
  }
  if (round.status !== 'RUNNING' || !round.clock.deadlineAt) return Math.max(0, round.clock.remainingMs);
  const deadline = Date.parse(round.clock.deadlineAt);
  return Number.isFinite(deadline) ? Math.max(0, deadline - estimatedServerNow) : 0;
}

function formatMilliseconds(milliseconds: number): string {
  const totalSeconds = Math.ceil(Math.max(0, milliseconds) / 1000);
  return `${Math.floor(totalSeconds / 60).toString().padStart(2, '0')}:${(totalSeconds % 60).toString().padStart(2, '0')}`;
}

export function Clock({ round, serverTime }: { round: RoundView; serverTime: string }) {
  const base = useMemo(() => ({ serverMs: Date.parse(serverTime), monoMs: performance.now() }), [serverTime, round.id, round.version, round.clock.deadlineAt, round.nextStartAt]);
  const [monoNow, setMonoNow] = useState(base.monoMs);
  useEffect(() => {
    setMonoNow(performance.now());
    const timer = window.setInterval(() => setMonoNow(performance.now()), 250);
    return () => window.clearInterval(timer);
  }, [base]);

  if (!Number.isFinite(base.serverMs)) return <div className="obs-clock" role="status"><span>时间校准中</span><strong>--:--</strong><small>等待新的权威快照</small></div>;
  const estimatedServerNow = base.serverMs + (monoNow - base.monoMs);
  const remaining = remainingClockMs(round, estimatedServerNow);
  if (round.status === 'INTERMISSION') return <div className="obs-clock"><span>轮间间歇</span><strong>{round.nextStartAt ? formatMilliseconds(remaining) : '--:--'}</strong><small>{remaining === 0 ? '等待下一轮确认 · 不自行开始新回合' : `距离下一轮${round.nextStartAt ? ` · 计划 ${round.nextStartAt}` : ''}`}</small></div>;
  if (round.status === 'DRAINING') return <div className="obs-clock"><span>剩余时间</span><strong>00:00</strong><small>正在收尾 · 等待完整权威结果</small></div>;
  const paused = round.status === 'PAUSED';
  const preparing = round.status === 'PREPARING';
  return <div className="obs-clock"><span>{paused ? '已暂停 · 剩余' : preparing ? '准备计时' : '剩余时间'}</span><strong>{preparing ? '--:--' : formatMilliseconds(remaining)}</strong><small>{paused ? '等待主播恢复' : preparing ? '等待展示与模型准备' : remaining === 0 ? '收尾中 · 等待权威结果' : '猜中即结算'}</small></div>;
}
