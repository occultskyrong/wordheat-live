import { useEffect } from 'react';
import type { DisplayView } from '../../api/fixture/types';
import { Feedback } from '../../features/guesses/Feedback';
import { Clock } from '../../features/round/Clock';
import { Reveal } from '../../features/round/Reveal';
import { PlayerRanking } from '../../features/ranking/PlayerRanking';
import { WordRanking } from '../../features/ranking/WordRanking';
import { obsDesignSize, useObsScale, type ObsLayout } from '../../features/display/useObsScale';
import '../../styles/obs.css';

function ObsHeader({ mode, runPurpose }: Pick<DisplayView, 'mode' | 'runPurpose'>) {
  const runLabel = { local: '本地演练', test_live: '直播测试', production: '正式直播' }[runPurpose];
  const modeLabel = mode === 'replay' ? '回放演练' : mode === 'official' && runPurpose === 'local' ? '正式场次' : mode === 'rehearsal' && runPurpose !== 'local' ? '排练' : null;
  return <header className="obs-header"><div><span className="obs-kicker">WORDHEAT / LIVE</span><h1>语义猜词</h1></div><div className="obs-demo-tag">{[runLabel, modeLabel, '合成数据'].filter(Boolean).join(' · ')}</div></header>;
}

type Props = {
  initialData: DisplayView;
  onRendered?: (feedbackId: string) => void;
  syncError?: string | null;
  layout?: ObsLayout;
  safeArea?: boolean;
  reducedMotion?: boolean;
  fontScale?: number;
};

export function ObsPage({
  initialData,
  onRendered,
  syncError,
  layout = 'landscape',
  safeArea = false,
  reducedMotion = false,
  fontScale = 1,
}: Props) {
  const data = initialData;
  const round = data.round;
  const reconnecting = data.transport === 'RECONNECTING';
  const unavailable = data.authorization === 'UNAUTHORIZED' || data.transport === 'FAULTED' || Boolean(syncError);
  const status = !round ? 'IDLE' : round.status;
  const scale = useObsScale(layout);
  const design = obsDesignSize(layout);
  const latestId = data.latest?.id;
  const statusText = {
    IDLE: '等待本轮开始', PREPARING: '本轮准备中', RUNNING: '本轮口令', PAUSED: '回合已暂停',
    DRAINING: '正在收尾', REVEALED: '本轮已揭晓', INTERMISSION: '等待下一轮确认', FAULTED: '回合故障',
  }[status];
  const statusDescription = status === 'RUNNING' && round
    ? `发送「猜 ${round.code} 苹果」参与；每条消息只猜一个词`
    : status === 'IDLE'
      ? '新一轮准备好后会在这里显示参与口令'
      : status === 'DRAINING'
        ? '本轮回合已关闭输入，等待入场与评分结果'
        : status === 'INTERMISSION'
          ? `下一轮尚未由服务端确认，不沿用旧口令${round?.nextStartAt ? `；计划开始：${new Date(round.nextStartAt).toLocaleString('zh-CN', { hour12: false })}` : ''}`
          : '当前状态只读展示，等待权威状态更新';

  useEffect(() => {
    if (latestId && !unavailable) onRendered?.(latestId);
  }, [latestId, unavailable, onRendered]);

  return <main
    className="obs-stage"
    data-synthetic="true"
    data-layout={layout}
    data-reduced-motion={reducedMotion ? 'true' : 'false'}
    data-font-scale={fontScale}
    style={{ placeItems: 'center' }}
  >
    <div className="obs-ambient obs-ambient-one" /><div className="obs-ambient obs-ambient-two" />
    <div
      className="obs-canvas"
      data-design={`${design.width}x${design.height}`}
      data-revealed={data.reveal ? 'true' : 'false'}
      data-long-hints={data.hints.some((hint) => hint.length > 60) ? 'true' : 'false'}
      style={{ width: design.width, height: design.height, transform: `scale(${scale.toFixed(6)})` }}
    >
      <ObsHeader mode={data.mode} runPurpose={data.runPurpose} />
      {data.authorization === 'UNAUTHORIZED' ? <section className="obs-system-state" role="alert"><h2>展示授权已失效</h2><p>旧画面已清除，请由主播重新生成一次性展示链接。</p></section> : data.transport === 'FAULTED' ? <section className="obs-system-state" role="alert"><h2>展示连接故障</h2><p>已停止展示更新与回执，请检查连接并重新打开。</p></section> : syncError === 'CONTRACT_VERSION_MISMATCH' ? <section className="obs-system-state" role="alert"><h2>展示版本不匹配</h2><p>请更新或重新打开展示页面</p><p>已停止旧画面更新与回执。</p></section> : syncError ? <section className="obs-system-state" role="alert"><h2>展示数据需要重新同步</h2><p>已停止旧画面更新与回执，请等待连续快照恢复。</p></section> : <>
        <section className="obs-rule"><span>{statusText}{round && status === 'RUNNING' && <strong>{round.code}</strong>}</span><p>{statusDescription}</p></section>
        {round && <section className="obs-round"><div><span className="obs-section-label">分类：{round.category}</span><div className="obs-blanks" aria-label={`答案 ${round.answerLength} 个字`}>{Array.from({ length: round.answerLength }, (_, index) => <i key={index} />)}</div><p>字格只表示答案长度</p></div><Clock round={round} serverTime={data.serverTime} /></section>}
        {reconnecting && <div className="obs-transport" role="status">数据重连中，回合时间继续；结果待确认</div>}
        <div className="obs-hints">{data.hints.length === 0 ? <span>暂无公开提示</span> : data.hints.map((hint, index) => <span key={hint}>提示 {index + 1} · {hint}</span>)}</div>
        {data.latest ? <Feedback feedback={data.latest} /> : <section className="obs-feedback obs-empty">暂无最新反馈</section>}
        <div className="obs-lower"><WordRanking words={data.wordRanking} /><PlayerRanking players={data.playerRanking} /></div>
        <Reveal reveal={data.reveal} />
      </>}
      <footer className="obs-footer"><span>词温 WORDHEAT</span><span>{unavailable ? '合成故障演练 · 未发送回执' : '相关度不是概率 · 精确命中由系统裁定'}</span></footer>
      {data.purpose === 'preview' && safeArea && <div className={`obs-safe-area obs-safe-area-${layout}`} aria-label="预览安全区" />}
    </div>
  </main>;
}
