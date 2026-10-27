import { useState } from 'react';
import { FlaskConical, LockKeyhole, Play } from 'lucide-react';
import { fixtureAdapter } from '../../api/fixture/adapter';
import type { DemoResult, ScoringView } from '../../api/fixture/types';
import { InlineNotice, PageHeader, Panel, StatusPill } from '../../components/ui';

type Props = {
  initialData: ScoringView;
  onCommand?: (command: string) => Promise<DemoResult>;
};

export function ScoringPage({ initialData, onCommand = fixtureAdapter.executeDemoCommand }: Props) {
  const [split, setSplit] = useState<'explore' | 'calibration' | 'blind'>('blind');
  const [guess, setGuess] = useState('橘子');
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const scoresVisible = split !== 'blind' || initialData.blindLocked;

  async function evaluate() {
    if (pending) return;
    setPending(true);
    setMessage('');
    try {
      const result = await onCommand('scoring_evaluation.create');
      setMessage(`${result.message} · 任务 eval_demo_01，未生成真实分数`);
    } catch (error) {
      setMessage(`${error instanceof Error ? error.message : '评测任务提交失败'} · 未生成真实分数`);
    } finally {
      setPending(false);
    }
  }

  return <div className="page">
    <PageHeader eyebrow="05 / SCORING LAB" title="评分实验室" description="固定模型、校准和题库版本；原始余弦与展示分分开审阅，合成样例不构成 G1-Q 质量结论。" actions={<StatusPill tone="warn">合成 fixture</StatusPill>} />
    <div className="grid sidebar-layout">
      <Panel title="新建评测" eyebrow="VERSIONED EVALUATION">
        <div className="form-grid">
          <label className="field"><span>目标题版本</span><select defaultValue="Q-018 · v4"><option>Q-018 · v4</option><option>Q-024 · v2</option></select></label>
          <label className="field"><span>固定 manifest</span><select defaultValue="demo-not-frozen"><option>demo-not-frozen</option></select></label>
          <label className="field"><span>对照词</span><input value={guess} onChange={(event) => setGuess(event.target.value)} /></label>
          <label className="field"><span>人工等级</span><select defaultValue="hot"><option>hot</option><option>warm</option><option>cold</option><option>exclude</option></select></label>
          <label className="field"><span>数据 split</span><select value={split} onChange={(event) => setSplit(event.target.value as typeof split)}><option value="explore">explore</option><option value="calibration">calibration</option><option value="blind">blind</option></select></label>
          <label className="field"><span>预处理／维度</span><input value="trim + lowercase · 512 dim" readOnly /></label>
        </div>
        <button className="button mt" disabled={!guess.trim() || pending} onClick={evaluate}><Play size={16} />{pending ? '评测提交中…' : '提交合成评测任务'}</button>
        <p className="caption mt">运行期间禁止重复提交；任务离页后应由后端继续管理。当前不会执行真实模型。</p>
        {message && <InlineNotice>{message}</InlineNotice>}
      </Panel>
      <Panel title="模型组合" eyebrow="FROZEN IDENTITY REQUIRED">
        <div className="data-list">
          <div className="data-row"><span>模型</span><strong>{initialData.model}</strong></div>
          <div className="data-row"><span>revision</span><strong>{initialData.revision}</strong></div>
          <div className="data-row"><span>校准</span><strong>{initialData.calibration}</strong></div>
          <div className="data-row"><span>题库</span><strong>{initialData.wordbankVersion}</strong></div>
        </div>
        <InlineNotice tone="warn">此 revision 明确未冻结，不可用于正式质量放行。</InlineNotice>
      </Panel>
    </div>

    <Panel title="评测结果" eyebrow="RAW COSINE ≠ DISPLAY SCORE" className="mt" action={<FlaskConical size={20} className="teal" />}>
      {!scoresVisible ? <div className="async-panel"><LockKeyhole size={24} /><span>盲标未锁定，模型分数已隐藏</span></div> : <div className="table-wrap"><table><thead><tr><th>对照词</th><th>原始余弦</th><th>展示分</th><th>系统等级</th><th>人工等级</th><th>精确命中</th><th>复核</th></tr></thead><tbody>{initialData.results.map((result) => <tr key={result.guess}><td><strong>{result.guess}</strong></td><td className="code">{result.raw.toFixed(3)}</td><td className="teal">{result.display.toFixed(1)}</td><td>{result.rating}</td><td>{result.rating === 'cold' ? 'cold' : 'hot'}</td><td>{result.exact ? '是' : '否'}</td><td><StatusPill tone="warn">待复核</StatusPill></td></tr>)}</tbody></table></div>}
      <p className="caption">展示分是相关度映射，非概率；精确命中是独立规则，不能由高分推断。原始余弦不格式化为百分比。</p>
    </Panel>
    <div className="grid two mt">
      <Panel title="差异与排序" eyebrow="REVIEW QUEUE"><p className="muted">正样本：橘子、雪梨；负样本：铁路。排序仅用于本次合成任务复核，不自动改变校准。</p></Panel>
      <Panel title="发布边界" eyebrow="SEPARATE PUZZLE COMMANDS"><p className="muted">候选 Q-018 v4 · {initialData.calibration}。批准与发布需另行权限及质检；活动轮不受影响。</p><button className="button secondary" disabled>质量未验收 · 禁止发布</button></Panel>
    </div>
  </div>;
}
