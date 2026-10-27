import { useState } from 'react';
import { Check, FlaskConical, Play, Radio, RotateCcw } from 'lucide-react';
import { fixtureAdapter } from '../../api/fixture/adapter';
import type { DemoResult, ReadinessView } from '../../api/fixture/types';
import { InlineNotice, ModeBadge, PageHeader, Panel, StatusPill } from '../../components/ui';

type Props = {
  initialData: ReadinessView;
  onCommand?: (command: string) => Promise<DemoResult>;
};

export function SetupPage({ initialData, onCommand = fixtureAdapter.executeDemoCommand }: Props) {
  const [sessionName, setSessionName] = useState('周末词温演练');
  const [pending, setPending] = useState<'create' | 'start' | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [message, setMessage] = useState('');
  const [realMessage, setRealMessage] = useState('');

  async function run(kind: 'create' | 'start') {
    setPending(kind);
    setMessage('');
    try {
      const result = await onCommand(kind === 'create' ? 'session.create' : 'session.start');
      if (kind === 'create') {
        setSessionId('session_demo_draft');
        setMessage(`草稿已创建：session_demo_draft · ${result.message}`);
      } else {
        setMessage(`演练已启动：session_demo_draft · ${result.message}`);
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '操作失败，请重试');
    } finally {
      setPending(null);
    }
  }

  return <div className="page">
    <PageHeader eyebrow="01 / GO LIVE CHECKLIST" title="开播准备" description="先创建独立场次草稿，再检查依赖并开始。所有结果均为合成演练，不代表平台或发行验收通过。" actions={<ModeBadge runPurpose="local" />} />
    <div className="grid sidebar-layout">
      <div className="stack">
        <Panel title="新建场次" eyebrow="STEP 01 · DRAFT">
          <div className="form-grid">
            <label className="field span-two"><span>场次名称</span><input value={sessionName} onChange={(event) => setSessionName(event.target.value)} /></label>
            <label className="field"><span>运行模式</span><select value="rehearsal" disabled><option value="rehearsal">本地 rehearsal</option></select></label>
            <label className="field"><span>运行用途</span><select value="local" disabled><option value="local">local · 合成演练</option></select></label>
            <label className="field"><span>输入模式</span><input value="人工试词 · rehearsal" readOnly /></label>
            <label className="field"><span>房间绑定</span><input value="本地演练无需绑定" readOnly /></label>
          </div>
          <div className="button-row mt">
            <button className="button" disabled={!sessionName.trim() || pending !== null} onClick={() => run('create')}><Check size={16} />{pending === 'create' ? '创建中…' : '创建场次草稿'}</button>
            <button className="button secondary" disabled={!sessionId || !initialData.capabilities.rehearse || pending !== null} onClick={() => run('start')}><Play size={16} />{pending === 'start' ? '开播准备中…' : '开始本地演练'}</button>
          </div>
          <p className="caption mt">创建只生成 CREATED 草稿；开始是另一条独立命令。创建与开始使用相同 run_purpose。</p>
          {message && <InlineNotice>{message}</InlineNotice>}
        </Panel>

        <Panel title="人工试词" eyebrow="STEP 03 · REHEARSAL ONLY">
          <div className="form-grid">
            <label className="field"><span>合成昵称</span><input defaultValue="演练观众" /></label>
            <label className="field"><span>试词</span><input defaultValue="橘子" /></label>
          </div>
          <div className="button-row mt"><button className="button secondary" disabled={!sessionId} onClick={() => setMessage('合成试词已记录：橘子 · 82.4；不是模型质量结论')}>提交合成试词</button><a className="button ghost" href="/obs.html" target="_blank" rel="noreferrer">打开布局预览</a></div>
          {!sessionId && <p className="caption mt">请先创建 rehearsal 场次；official 场次不能手工注入。</p>}
        </Panel>

        <Panel title="平台连接" eyebrow="CONTROLLED CONNECTOR">
          <InlineNotice tone="warn">真实 connector 尚未接入。本页不会把合成数据显示为“自动弹幕已接通”。</InlineNotice>
          <div className="button-row mt"><button className="button secondary" onClick={async () => { const result = await fixtureAdapter.requestRealCapability(); setRealMessage(result.message); }}><Radio size={16} />申请平台测试</button></div>
          <p className="caption mt">平台测试 · 待真实 API 接入；test_live 仍需单独授权，不能自动转为 production。</p>
          {realMessage && <InlineNotice>{realMessage}</InlineNotice>}
        </Panel>
      </div>

      <div className="stack">
        <Panel title="运行环境" eyebrow="SAMPLED READINESS" action={<button className="icon-button" aria-label="重试检测" onClick={() => setMessage('已刷新合成 readiness；未触发下载')}><RotateCcw size={16} /></button>}>
          <div className="data-list">{initialData.checks.map((check) => <div className="data-row" key={check.name}><span>{check.name}<small>{check.detail}</small></span><StatusPill tone={check.status === 'pass' ? 'good' : check.status === 'fail' ? 'bad' : 'warn'}>{check.status}</StatusPill></div>)}</div>
          <p className="caption mt">采样：{initialData.runtime.sampledAt}<br />{initialData.runtime.model} · {initialData.runtime.schema}</p>
        </Panel>
        <Panel title="验收门禁" eyebrow="EVIDENCE, NOT UI STATE">
          <div className="data-list">{initialData.gates.map((gate) => <div className="data-row" key={gate.name}><span>{gate.name}<small>{gate.detail}</small></span><StatusPill tone={gate.status === 'pass' ? 'good' : 'warn'}>{gate.status}</StatusPill></div>)}</div>
        </Panel>
        <Panel title="当前可用能力" eyebrow="THREE SEPARATE GATES">
          <div className="data-list">
            <div className="data-row"><span>本地演练</span><StatusPill tone="good">可演练</StatusPill></div>
            <div className="data-row"><span>平台测试</span><StatusPill>待接入</StatusPill></div>
            <div className="data-row"><span>正式开播</span><StatusPill>未放行</StatusPill></div>
          </div>
          <p className="caption mt"><FlaskConical size={13} /> 创建场次 → 检测 → 演练／测试 → 预览 → 开始</p>
        </Panel>
      </div>
    </div>
  </div>;
}
