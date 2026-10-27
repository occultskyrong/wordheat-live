import type { DisplayView } from '../../api/fixture/types';

export function PlayerRanking({ players }: { players: DisplayView['playerRanking'] }) {
  return <section className="obs-players"><h2>场次前三</h2><div>{players.length === 0 ? <p className="obs-empty">暂无场次积分</p> : players.map((player, index) => <article key={player.id}><span title={player.name}>0{index + 1} {player.name}</span><strong>{player.points}<small> 分</small></strong></article>)}</div></section>;
}
