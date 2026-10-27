import { useState } from 'react';
import { CirclePause, Play, RotateCcw, Search, Square } from 'lucide-react';
import { fixtureAdapter } from '../../api/fixture/adapter';
import type { DemoResult, DiagnosticsView } from '../../api/fixture/types';
import { ConfirmDialog, InlineNotice, ModeBadge, PageHeader, Panel, StatusPill } from '../../components/ui';

type Props = {
  initialData: DiagnosticsView;
  onCommand?: (command: string) => Promise<DemoResult>;
};

export function ReplayPage({ initialData, onCommand = fixtureAdapter.executeDemoCommand }: Props) {
  const [messageId, setMessageId] = useState('msg_demo_001');
  const [session, setSession] = useState('session_demo');
  const [reason, setReason] = useState('全部');
  const [cursor, setCursor] = useState('');
  const [job, setJob] = useState(initialData.replay);
  const [confirmAction, setConfirmAction] = useState<'create' | 'cancel' | null>(null);
  const [message, setMessage] = useState('');
  const [pending, setPending] = useState<string | null>(null);
  const filteredMessages = initialData.messages.filter((item) => reason === '全部' || item.reason === reason);
  const activeJob = ['created', 'running', 'paused'].includes(job.status);

  async function command(name: string) {
    if (pending) return;
    setPending(name);
    setMessage('');
    try {
      const result = await onCommand(name);
      if (result.status === 'applied') {
        const status = name === 'replay.start' || name === 'replay.resume' ? 'running' : name === 'replay.pause' ? 'paused' : name === 'replay.cancel' ? 'canceled' : job.status;
        setJob((current) => ({ ...current, status }));
      }
      setMessage(`${result.message} · 等待权威任务状态`);
    } catch (error) {
      setMessage(`${error instanceof Error ? error.message : '回放命令失败'} · 保留当前权威任务状态`);
    } finally {
      setPending(null);
      setConfirmAction(null);
    }
  }

  return <div className="page">
    <PageHeader eyebrow="06 / TRACE & ISOLATED REPLAY" title="运行诊断与回放" description="正式只读追踪与隔离回放分区呈现；没有证据的阶段保持未知，不由前端补齐成功。" actions={<ModeBadge runPurpose="local" />} />
    <div className="grid two">
      <Panel title="正式只读追踪" eyebrow="READ ONLY · SESSION_DEMO">
        <div className="form-grid">
          <label className="field"><span>session</span><input value={session} onChange={(event) => setSession(event.target.value)} /></label>
          <label className="field"><span>message_id</span><input value={messageId} onChange={(event) => setMessageId(event.target.value)} /></label>
          <label className="field"><span>reason</span><select value={reason} onChange={(event) => setReason(event.target.value)}><option>全部</option><option>SCORED</option><option>DUPLICATE</option></select></label>
          <label className="field"><span>cursor</span><input value={cursor} onChange={(event) => setCursor(event.target.value)} placeholder="可选分页游标" /></label>
        </div>
        <button className="button secondary mt" disabled={!session.trim()} onClick={() => setMessage(`已查询 ${session} / ${messageId} · 合成追踪${cursor ? ` · cursor ${cursor}` : ''}`)}><Search size={16} />查询脱敏追踪</button>
        <div className="table-wrap mt"><table><thead><tr><th>message_id</th><th>猜词</th><th>reason</th><th>接收时刻</th></tr></thead><tbody>{filteredMessages.map((item) => <tr key={item.id}><td><button className="text-link" onClick={() => setMessageId(item.id)}>{item.id}</button></td><td>{item.word}</td><td>{item.reason}</td><td>{item.receivedAt}</td></tr>)}</tbody></table></div>
        <InlineNotice>若平台未推送，本系统未收到，无法判断上游遗漏。原始 UID 与密钥不在此视图中。</InlineNotice>
      </Panel>
      <Panel title="链路证据" eyebrow={messageId}>
        <div className="timeline">{initialData.trace.map((step) => <div className={`timeline-item ${step.status}`} key={step.stage}><div className="timeline-marker"><span /></div><div className="timeline-content"><strong>{step.stage}</strong><small>{step.evidence}</small></div><div><span className="timeline-time">{step.time}</span><StatusPill tone={step.status === 'done' ? 'good' : step.status === 'failed' ? 'bad' : 'warn'}>{step.status}</StatusPill></div></div>)}</div>
      </Panel>
    </div>

    <Panel title="连接与窗口状态" eyebrow="PRIVATE READ ONLY" className="mt">
      <div className="grid four">
        <div className="data-row"><span>队列积压</span><strong>2 · 合成</strong></div>
        <div className="data-row"><span>relay gap</span><strong>0 · 合成</strong></div>
        <div className="data-row"><span>租约／代次</span><strong>15s / 12</strong></div>
        <div className="data-row"><span>窗口 epoch</span><strong>epoch_demo_7</strong></div>
      </div>
    </Panel>

    <Panel title="隔离回放" eyebrow="TARGET MODE: REPLAY" className="mt">
      <div className="form-grid">
        <label className="field"><span>记录来源</span><select defaultValue="合成脱敏记录"><option>合成脱敏记录</option><option>获授权脱敏记录</option></select></label>
        <label className="field"><span>回放速度</span><select defaultValue="1×"><option>0.5×</option><option>1×</option><option>2×</option></select></label>
      </div>
      <p className="caption mt">目标 mode：replay · 不会向正式任务、榜单或主展示投递，不产生平台履约。</p>
      <div className="button-row">
        <button className="button" disabled={pending !== null} onClick={() => setConfirmAction('create')}><Play size={16} />创建隔离回放</button>
        <button className="button secondary" disabled={pending !== null || job.status !== 'created'} onClick={() => command('replay.start')}><Play size={16} />开始回放</button>
        <button className="button secondary" disabled={pending !== null || job.status !== 'running'} onClick={() => command('replay.pause')}><CirclePause size={16} />暂停</button>
        <button className="button secondary" disabled={pending !== null || job.status !== 'paused'} onClick={() => command('replay.resume')}><RotateCcw size={16} />恢复</button>
        <button className="button danger" disabled={pending !== null || !activeJob} onClick={() => setConfirmAction('cancel')}><Square size={16} />取消回放</button>
      </div>
      <div className="mt"><div className="data-row"><span>{job.id} · {job.status}</span><strong>{job.done} / {job.total}</strong></div><div className="progress-track"><span style={{ width: `${job.total > 0 ? job.done / job.total * 100 : 0}%` }} /></div></div>
      {message && <InlineNotice>{message}</InlineNotice>}
    </Panel>
    {confirmAction === 'create' && <ConfirmDialog title="创建隔离回放" description="确认记录来源已获许可，且不会向正式任务、榜单或主展示投递。目标 mode 固定为 replay。" confirmLabel="确认创建回放" pending={pending === 'replay.create'} onCancel={() => setConfirmAction(null)} onConfirm={() => command('replay.create')} />}
    {confirmAction === 'cancel' && <ConfirmDialog title="取消回放" description={`确认取消 ${job.id}？取消后等待权威任务终态，不在浏览器假报停止成功。`} confirmLabel="确认取消回放" pending={pending === 'replay.cancel'} onCancel={() => setConfirmAction(null)} onConfirm={() => command('replay.cancel')} />}
  </div>;
}
