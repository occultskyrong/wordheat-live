import type { FeedbackView } from '../../api/fixture/types';

export function Feedback({ feedback }: { feedback: FeedbackView }) {
  return <article className={`obs-feedback${(feedback.word?.length ?? 0) > 24 ? ' obs-feedback-long' : ''}`} data-feedback-id={feedback.id} aria-live="polite"><div><span className="obs-section-label">最新有效猜词</span><strong className="obs-feedback-word">{feedback.word ?? '未形成有效猜词'}</strong></div><div className="obs-feedback-score">{feedback.score === null ? '—' : feedback.score.toFixed(1)}<small>{feedback.solved ? '精确命中' : feedback.message}</small></div><div className="obs-feedback-user"><strong>{feedback.name}</strong><span>{feedback.solved ? '本轮赢家' : feedback.score === null ? '等待下一条' : '更接近 ↑'}</span></div></article>;
}
