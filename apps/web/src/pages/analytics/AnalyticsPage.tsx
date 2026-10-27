import { useState } from 'react';
import { Download, ExternalLink } from 'lucide-react';
import type { AnalyticsView } from '../../api/fixture/types';
import { ConfirmDialog, InlineNotice, MetricCard, ModeBadge, PageHeader, Panel } from '../../components/ui';
import { safeCsvFilename, serializeCsv } from '../../security/csv';

export function AnalyticsPage({ initialData, sessionId, stale = false }: { initialData: AnalyticsView; sessionId: string; stale?: boolean }) {
  const [confirmExport, setConfirmExport] = useState(false);
  const [message, setMessage] = useState('');

  function exportCsv() {
    if (stale) return;
    const csv = serializeCsv(initialData.rounds.map((round) => ({ round_id: round.id, reason: round.reason, duration: round.duration, hints: round.hints, status: round.status })), ['round_id', 'reason', 'duration', 'hints', 'status']);
    const url = URL.createObjectURL(new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = safeCsvFilename(sessionId, initialData.sampledAt.slice(0, 10));
    link.click();
    URL.revokeObjectURL(url);
    setConfirmExport(false);
    setMessage(`已生成脱敏 CSV · 实际统计时点 ${initialData.sampledAt} · 合成数据`);
  }

  return <div className="page analytics-page">
    <PageHeader eyebrow="07 / SESSION RETROSPECTIVE" title="场次复盘" description={`${sessionId} · 统计时点：${initialData.sampledAt} · 未终态任务 ${initialData.pendingJobs}`} actions={<><ModeBadge runPurpose={initialData.runPurpose} /><button className="button secondary" disabled={stale} onClick={() => setConfirmExport(true)}><Download size={16} />导出脱敏 CSV</button></>} />
    {stale && <InlineNotice tone="warn">统计数据已过期；保留 {initialData.sampledAt} 的内容供查看，导出已禁用。</InlineNotice>}
    <InlineNotice tone="warn">local 演练与 replay 数据隔离，不计入正式指标；test_live 也不会被浏览器改标为 production。</InlineNotice>
    <div className="grid three mt analytics-metrics">{initialData.metrics.map((metric, index) => <MetricCard key={metric.label} label={metric.label} value={metric.value} note={metric.note} accent={index === 0} />)}</div>
    <div className="grid sidebar-layout mt">
      <Panel title="参与趋势" eyebrow="BACKEND ANALYTICS · SYNTHETIC">
        <div className="chart" aria-label="每分钟有效参与合成趋势">{initialData.trend.map((point) => <div className="chart-column" key={point.label}><strong>{point.value}</strong><span className="chart-bar" style={{ height: `${point.value / 40 * 150}px` }} /><small>{point.label}</small></div>)}</div>
        <p className="caption">仅展示后端 analytics 返回口径，不由公开前八词推断全场数据。</p>
      </Panel>
      <Panel title="统计边界" eyebrow="NO INVENTED DENOMINATOR">
        <p className="muted">昵称数不是观看人数。没有平台观看分母时，不提供漏词率、停留率或增长结论。</p>
        <div className="data-row"><span>来源用途</span><strong>{initialData.runPurpose}</strong></div>
        <div className="data-row"><span>未终态任务</span><strong>{initialData.pendingJobs}</strong></div>
      </Panel>
    </div>
    <Panel title="回合明细" eyebrow="ROUND LIFECYCLE" className="mt">
      <div className="table-wrap"><table><thead><tr><th>round_id</th><th>终结 reason</th><th>用时</th><th>提示数</th><th>状态</th><th>追踪</th></tr></thead><tbody>{initialData.rounds.map((round) => <tr key={round.id}><td><strong>{round.id}</strong></td><td>{round.reason}</td><td>{round.duration}</td><td>{round.hints}</td><td>{round.status}</td><td><a className="text-link" href="/replay">只读详情<ExternalLink size={13} /></a></td></tr>)}</tbody></table></div>
    </Panel>
    {message && <div className="mt"><InlineNotice>{message}</InlineNotice></div>}
    {confirmExport && <ConfirmDialog title="导出脱敏 CSV" description={`来源：local · 合成演练；统计时点 ${initialData.sampledAt}。字段白名单：round_id、reason、duration、hints、status。不含私有答案、原始 UID 或平台 secret。`} confirmLabel="确认导出 CSV" onCancel={() => setConfirmExport(false)} onConfirm={exportCsv} />}
  </div>;
}
