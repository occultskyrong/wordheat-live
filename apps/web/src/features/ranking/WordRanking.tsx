import type { DisplayView } from '../../api/fixture/types';

export function WordRanking({ words }: { words: DisplayView['wordRanking'] }) {
  return <section className="obs-ranking"><div className="obs-ranking-heading"><h2>近词榜</h2><span>展示分 / 100</span></div>{words.length === 0 ? <p className="obs-empty">暂无有效猜词，发送猜词参与本轮</p> : <ol>{words.slice(0, 8).map((item, index) => <li key={`${item.word}-${item.name}`}><span className="obs-rank">{String(index + 1).padStart(2, '0')}</span><strong title={item.word}>{item.word}</strong><span className="obs-word-score">{item.score.toFixed(1)}</span><span className="obs-word-user" title={`${item.name} · ${item.count} 人`}>{item.name} · {item.count} 人</span><i style={{ width: `${item.score}%` }} /></li>)}</ol>}</section>;
}
