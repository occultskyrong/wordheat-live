import type { DisplayView } from '../../api/fixture/types';

export const publicDisplayFixture: DisplayView = {
  synthetic: true,
  sessionId: 'session_demo',
  mode: 'rehearsal',
  runPurpose: 'local',
  serverTime: '2026-10-25T12:00:00.000Z',
  asOfSeq: '9007199254740993',
  authorization: 'AUTHORIZED',
  purpose: 'preview',
  renderGeneration: '12',
  round: {
    id: 'round_demo_7', code: 'A7', category: '水果', answerLength: 2,
    status: 'RUNNING', remainingSeconds: 138, version: 3,
    clock: { timeOrigin: 'local_rehearsal', deadlineAt: '2026-10-25T12:02:18.000Z', remainingMs: 138000 },
  },
  transport: 'CONNECTED',
  hints: ['常见水果', '果皮常用来做饮品或调味'],
  latest: { id: 'feedback_demo_1', word: '橘子', name: '小林', score: 82.4, status: 'scored', solved: false, message: '很接近，再想想酸甜口感' },
  feedback: [
    { id: 'feedback_demo_1', word: '橘子', name: '小林', score: 82.4, status: 'scored', solved: false, message: '很接近' },
    { id: 'feedback_demo_2', word: '果汁', name: '木木', score: 64.1, status: 'scored', solved: false, message: '有关联' },
    { id: 'feedback_demo_3', word: null, name: '阿泽', score: null, status: 'duplicate', solved: false, message: '这条猜词已重复' },
  ],
  wordRanking: [
    { word: '橘子', name: '小林', count: 4, score: 82.4 },
    { word: '雪梨', name: '阿泽', count: 3, score: 79.6 },
    { word: '桃子', name: '圆圆', count: 2, score: 76.8 },
    { word: '柚子', name: '小夏', count: 2, score: 73.2 },
    { word: '果园', name: '北北', count: 5, score: 69.5 },
    { word: '果汁', name: '木木', count: 3, score: 64.1 },
    { word: '甜味', name: '橙子同学', count: 1, score: 60.3 },
    { word: '树枝', name: '小鱼', count: 1, score: 41.7 },
  ],
  playerRanking: [{ id: 'participant_demo_1', name: '小林', points: 120 }, { id: 'participant_demo_2', name: '阿泽', points: 96 }, { id: 'participant_demo_3', name: '圆圆', points: 88 }],
  reveal: null,
  authorizedDeliveryIds: ['delivery_demo_1'],
};
