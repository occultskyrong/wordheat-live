import type { DisplayView } from '../../api/fixture/types';

export function Reveal({ reveal }: { reveal: DisplayView['reveal'] }) {
  if (!reveal) return null;
  return <section className="obs-reveal"><span>本轮揭晓 · {reveal.reason}</span><strong>{reveal.word}</strong><p>{reveal.explanation}</p><small>{reveal.winner ? `赢家：${reveal.winner}` : '本轮无人猜中'}</small></section>;
}
