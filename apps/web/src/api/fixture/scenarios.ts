import type { DisplayView } from './types';

export type FixtureScenario = 'normal' | 'loading' | 'empty' | 'partial-error' | 'forbidden' | 'contract-error';
export type DisplayScenario = FixtureScenario | 'preparing' | 'draining' | 'revealed' | 'intermission' | 'long-content' | 'faulted' | 'unauthorized';

const fixtureScenarios: FixtureScenario[] = ['loading', 'empty', 'partial-error', 'forbidden', 'contract-error'];
const displayScenarios: DisplayScenario[] = [...fixtureScenarios, 'preparing', 'draining', 'revealed', 'intermission', 'long-content', 'faulted', 'unauthorized'];

export function readFixtureScenario(search: string): FixtureScenario {
  const value = new URLSearchParams(search).get('scenario');
  return fixtureScenarios.includes(value as FixtureScenario) ? value as FixtureScenario : 'normal';
}

export function readDisplayScenario(search: string): DisplayScenario {
  const value = new URLSearchParams(search).get('scenario');
  return displayScenarios.includes(value as DisplayScenario) ? value as DisplayScenario : 'normal';
}

export function applyDisplayScenario(view: DisplayView, scenario: DisplayScenario): DisplayView {
  const data = structuredClone(view);
  if (scenario === 'empty') {
    return { ...data, round: null, hints: [], latest: null, feedback: [], wordRanking: [], playerRanking: [], reveal: null, authorizedDeliveryIds: [] };
  }
  if (scenario === 'forbidden' || scenario === 'unauthorized') return { ...data, authorization: 'UNAUTHORIZED', authorizedDeliveryIds: [] };
  if (scenario === 'faulted') return { ...data, transport: 'FAULTED', authorizedDeliveryIds: [] };
  if (scenario === 'partial-error') return { ...data, transport: 'RECONNECTING' };
  if (scenario === 'preparing' || scenario === 'draining' || scenario === 'intermission') {
    const status = scenario.toUpperCase() as 'PREPARING' | 'DRAINING' | 'INTERMISSION';
    return { ...data, round: data.round ? { ...data.round, status, reason: status === 'INTERMISSION' ? 'TIMEOUT' : data.round.reason, nextStartAt: status === 'INTERMISSION' ? '2026-10-25T12:03:00.000Z' : null } : null };
  }
  if (scenario === 'revealed') {
    return {
      ...data,
      round: data.round ? { ...data.round, status: 'REVEALED', reason: 'TIMEOUT', remainingSeconds: 0 } : null,
      reveal: { word: '杨桃', explanation: '一种横切面常呈五角星形的水果；这是用于检查公开揭晓排版的合成解释，不代表真实题库或评分结果。', winner: null, reason: 'TIMEOUT' },
      authorizedDeliveryIds: [],
    };
  }
  if (scenario === 'long-content') {
    const longWord = '超级无敌长猜词SupercalifragilisticexpialidociousWithoutAnyBreaks';
    const longName = '特别长的观众昵称<img src=x onerror=alert(1)>Tail';
    return {
      ...data,
      hints: ['这是用于验证长提示自动换行且不会推走参与规则与最新反馈的合成提示文本，'.repeat(3), '<img src=x onerror=alert(1)>只应作为纯文本显示', '第三阶段提示'],
      latest: data.latest ? { ...data.latest, word: longWord, name: longName, message: '长文本与注入字符只读展示' } : null,
      feedback: data.latest ? [{ ...data.latest, word: longWord, name: longName }] : [],
      wordRanking: data.wordRanking.map((item, index) => index === 0 ? { ...item, word: longWord, name: longName } : item),
      playerRanking: data.playerRanking.map((item, index) => index === 0 ? { ...item, name: longName } : item),
    };
  }
  return data;
}
