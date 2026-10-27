import { useState } from 'react';
import { Eye, EyeOff, FastForward, Pause, Play, Square } from 'lucide-react';
import { fixtureAdapter } from '../../api/fixture/adapter';
import type { DemoResult, RoundStatus, StudioView } from '../../api/fixture/types';
import { ConfirmDialog, InlineNotice, MetricCard, ModeBadge, PageHeader, Panel, StatusPill } from '../../components/ui';
import { WordRanking } from '../../features/ranking/WordRanking';
import { PlayerRanking } from '../../features/ranking/PlayerRanking';

type Props = {
  initialData: StudioView;
  onCommand?: (command: string) => Promise<DemoResult>;
};

type ConfirmAction = {
  command: string;
  title: string;
  description: string;
  confirmLabel: string;
};

export function StudioPage({ initialData, onCommand = fixtureAdapter.executeDemoCommand }: Props) {
  const [answerVisible, setAnswerVisible] = useState(false);
  const [round, setRound] = useState(initialData.display.round);
  const [nextHint, setNextHint] = useState(initialData.nextHint);
  const [pending, setPending] = useState<string | null>(null);
  const [message, setMessage] = useState('');
  const [confirm, setConfirm] = useState<ConfirmAction | null>(null);
  const display = initialData.display;
  const reconnecting = display.transport === 'RECONNECTING';
  const roundStatus = round?.status ?? 'IDLE';
  const commandsFrozen = reconnecting || pending !== null;
  const canCreate = roundStatus === 'IDLE' || roundStatus === 'REVEALED' || roundStatus === 'INTERMISSION';
  const canChangeRound = roundStatus === 'RUNNING' || roundStatus === 'PAUSED';

  function updateStatus(status: RoundStatus) {
    setRound((current) => current ? { ...current, status } : current);
  }

  async function command(name: string) {
    if (pending) return;
    setPending(name);
    setMessage('');
    try {
      const result = await onCommand(name);
      if (result.status === 'applied') {
        if (name === 'round.pause') updateStatus('PAUSED');
        if (name === 'round.resume') updateStatus('RUNNING');
        if (name === 'round.skip' || name === 'session.end') updateStatus('DRAINING');
        if (name === 'round.void') updateStatus('REVEALED');
        if (name === 'round.reveal_hint') setNextHint(null);
      }
      setMessage(result.message);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '命令失败，状态未被本地改写');
    } finally {
      setPending(null);
      setConfirm(null);
    }
  }

  function requestConfirmation(action: ConfirmAction) {
    setMessage('');
    setConfirm(action);
  }

  return <div className="page">
    <PageHeader eyebrow="03 / LIVE CONTROL" title="主播控制台" description={`场次 ${initialData.sessionId} · 当前轮 ${round?.id ?? '尚未开始'} · 后端版本 ${round ? `v${round.version}` : '—'}`} actions={<><ModeBadge runPurpose={display.runPurpose} /><StatusPill tone={reconnecting ? 'warn' : 'good'}>{display.transport}</StatusPill></>} />
    {reconnecting && <InlineNotice tone="warn">重连中，回合计时继续；依赖旧租约的命令已冻结，结果待权威状态确认。</InlineNotice>}
    <div className="grid four mt studio-metrics">
      <MetricCard label="当前回合" value={roundStatus} note={round ? `${round.code} · ${round.category}` : '暂无回合'} accent />
      <MetricCard label="接收消息" value={String(initialData.queue.received)} note="合成接收计数" />
      <MetricCard label="已评分" value={String(initialData.queue.scored)} note={`待处理 ${initialData.queue.pending}`} />
      <MetricCard label="展示已处理" value={String(initialData.queue.displayed)} note="processed 才释放额度" />
    </div>
    <div className="grid sidebar-layout mt">
      <div className="stack">
        <Panel title="回合操作" eyebrow={round ? `${round.id} · EXPECTED VERSION ${round.version}` : 'NO ACTIVE ROUND'}>
          <div className="button-row">
            <button className="button" disabled={!canCreate || commandsFrozen} onClick={() => command('round.create')}><Play size={16} />{canCreate && roundStatus !== 'IDLE' ? '开始下一轮' : '开始新轮'}</button>
            <button className="button amber" disabled={roundStatus !== 'RUNNING' || commandsFrozen} onClick={() => command('round.pause')}><Pause size={16} />{pending === 'round.pause' ? '暂停处理中…' : '暂停'}</button>
            <button className="button" disabled={roundStatus !== 'PAUSED' || commandsFrozen} onClick={() => command('round.resume')}><Play size={16} />{pending === 'round.resume' ? '恢复处理中…' : '恢复'}</button>
            <button className="button secondary" disabled={!canChangeRound || commandsFrozen} onClick={() => requestConfirmation({ command: 'round.skip', title: '跳过本轮', confirmLabel: '确认跳过本轮', description: `确认跳过 ${round?.id ?? '当前轮'}？此操作会冲销本轮积分，随后等待权威 VOID 与收尾，不会直接清榜。` })}><FastForward size={16} />跳题</button>
            <button className="button secondary" disabled={!canChangeRound || commandsFrozen} onClick={() => requestConfirmation({ command: 'round.void', title: '作废本轮', confirmLabel: '确认作废本轮', description: '确认作废当前轮？此操作会冲销本轮积分，并等待权威揭晓与结算。' })}>作废本轮</button>
            <button className="button secondary" disabled={roundStatus !== 'RUNNING' || !nextHint || commandsFrozen} onClick={() => requestConfirmation({ command: 'round.reveal_hint', title: '发布预审提示', confirmLabel: '确认发布预审提示', description: `确认发布以下提示？${nextHint ?? ''}` })}>发布预审提示</button>
            {roundStatus === 'FAULTED' && <button className="button amber" disabled={commandsFrozen} onClick={() => requestConfirmation({ command: 'session.recover', title: '恢复场次', confirmLabel: '确认恢复场次', description: '确认执行故障恢复？恢复后等待权威快照，不会自行改为 RUNNING。' })}>恢复场次</button>}
            <button className="button danger" disabled={commandsFrozen} onClick={() => requestConfirmation({ command: 'session.end', title: '结束场次', confirmLabel: '确认结束场次', description: '确认结束当前场次？将停止任务、关闭输入并等待收尾，不能通过关闭浏览器替代。' })}><Square size={16} />{pending === 'session.end' ? '停播中…' : '结束场次'}</button>
          </div>
          <p className="caption mt">当前剩余 {round?.remainingSeconds ?? 0} 秒 · 状态仅以权威快照为准；本地演练结果不代表真实回合已变更。</p>
          {roundStatus === 'PAUSED' && <InlineNotice tone="warn">回合已暂停 · 合成演练</InlineNotice>}
          {roundStatus === 'DRAINING' && <InlineNotice tone="warn">正在收尾，输入已关闭；禁止暂停、恢复或新轮，等待权威结算。</InlineNotice>}
          {(roundStatus === 'REVEALED' || roundStatus === 'INTERMISSION') && <InlineNotice>回合原因：{round?.reason ?? display.reveal?.reason ?? '等待权威结算原因'}；可开始下一轮，旧短码不会沿用。</InlineNotice>}
          {roundStatus === 'FAULTED' && <InlineNotice tone="bad">回合故障：{round?.reason ?? '等待故障详情'}。恢复场次需要确认，恢复后等待权威快照。</InlineNotice>}
          {message && <InlineNotice>{message}</InlineNotice>}
        </Panel>
        <Panel title="输入与处理健康" eyebrow="FOUR INDEPENDENT SIGNALS">
          <div className="grid four">
            <div><span className="field-label">Relay</span><StatusPill tone={reconnecting ? 'warn' : 'good'}>{reconnecting ? '重连中' : '合成已连接'}</StatusPill></div>
            <div><span className="field-label">本机处理</span><StatusPill tone="good">合成正常</StatusPill></div>
            <div><span className="field-label">模型</span><StatusPill tone="good">合成已预热</StatusPill></div>
            <div><span className="field-label">Display</span><StatusPill tone="good">preview</StatusPill></div>
          </div>
        </Panel>
        <div className="grid two">
          <Panel title="接近词榜" eyebrow="PUBLIC PROJECTION"><WordRanking words={display.wordRanking} /></Panel>
          <Panel title="玩家榜" eyebrow="SESSION POINTS"><PlayerRanking players={display.playerRanking} /></Panel>
        </div>
      </div>
      <div className="stack">
        <Panel title="私有谜底" eyebrow="PRIVATE STATE">
          <div className={`secret-box ${answerVisible ? 'revealed' : ''}`}>{answerVisible ? initialData.privateAnswer : '••••••'}</div>
          <button className="button secondary mt" onClick={() => setAnswerVisible((visible) => !visible)}>{answerVisible ? <EyeOff size={16} /> : <Eye size={16} />}{answerVisible ? '重新遮罩' : '查看谜底'}</button>
          {answerVisible && <p className="caption amber mt">私有，勿捕获</p>}
          <p className="caption mt">遮罩只是防误看，不是权限边界；OBS 公开入口不加载此模块。</p>
        </Panel>
        <Panel title="下一题预览" eyebrow="DEFAULT COLLAPSED">
          <p className="muted">默认折叠。切轮或切场后重新遮罩。</p>
          <button className="button ghost" onClick={() => setMessage('下一题为私有信息；当前仅展示合成占位，不生成真实抽题')}>展开合成占位</button>
        </Panel>
        <Panel title="场次边界" eyebrow="SESSION LIFECYCLE">
          <p className="caption">积分按当前场次周期计算。结束场次会停止任务、关闭输入并等待收尾；关闭浏览器不等于停播。</p>
          <div className="data-row"><span>题库</span><strong>100 道合成已发布</strong></div>
          <div className="data-row"><span>展示代次</span><strong>generation 12</strong></div>
        </Panel>
      </div>
    </div>
    {confirm && <ConfirmDialog title={confirm.title} description={confirm.description} confirmLabel={confirm.confirmLabel} pending={pending === confirm.command} onCancel={() => setConfirm(null)} onConfirm={() => command(confirm.command)} />}
  </div>;
}
