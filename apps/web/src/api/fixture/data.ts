import type { AnalyticsView, DiagnosticsView, ReadinessView, ScoringView, SettingsView, StudioView, WordbankView } from './types';
import { publicDisplayFixture } from '../../test/fixtures/display';

export const displayFixture = publicDisplayFixture;

export const readinessFixture: ReadinessView = {
  synthetic: true,
  checks: [
    { name: '本地数据库', status: 'pass', detail: '合成 schema v1 已连接' },
    { name: '评分模型', status: 'pass', detail: '合成 BGE 预热记录' },
    { name: '发布题库', status: 'pass', detail: '合成 100 道已审题' },
    { name: '公网中继', status: 'fail', detail: '真实连接尚未接入' },
    { name: '房间租约', status: 'pending', detail: '等待真实授权' },
    { name: '主展示', status: 'pass', detail: '合成 preview 已绑定' },
  ],
  gates: [
    { name: 'G0 平台可行性', status: 'pending', detail: '待真实平台证据' },
    { name: 'G1-Q 评分质量', status: 'pending', detail: '待真实盲评' },
    { name: 'G1-R 共机资源', status: 'pending', detail: '待 Windows + OBS 实测' },
    { name: 'G2 真实直播', status: 'pending', detail: '待固定 RC 采证' },
    { name: 'RC 发行候选', status: 'pending', detail: '尚未冻结' },
  ],
  capabilities: { rehearse: true, testLive: false, goLive: false },
  runtime: { launcher: 'READY_REHEARSAL', model: 'bge-small-zh-v1.5 · demo', schema: 'schema v1', sampledAt: '2026-10-25 19:58:00' },
};

export const studioFixture: StudioView = {
  synthetic: true,
  sessionId: 'session_demo',
  privateAnswer: '柠檬',
  nextHint: '果皮常用来做饮品或调味',
  display: displayFixture,
  readiness: readinessFixture,
  queue: { received: 18, scored: 16, displayed: 14, pending: 2 },
};

export const wordbankFixture: WordbankView = {
  synthetic: true,
  puzzles: [
    { id: 'Q-018', word: '柠檬', category: '水果', difficulty: 2, state: '已发布', version: 4, aliases: ['柠檬水'], hints: ['果皮常用来做饮品或调味', '味道偏酸', '常见黄色水果'], explanation: '一种常见的柑橘类水果。', calibration: 'cal-v3', source: '合成授权题库示例' },
    { id: 'Q-024', word: '萤火', category: '自然', difficulty: 3, state: '待审核', version: 2, aliases: ['萤火虫光'], hints: ['夜晚可见', '会发光', '与昆虫有关'], explanation: '萤火虫发出的光。', calibration: 'cal-v3', source: '合成内容样例' },
    { id: 'Q-031', word: '风筝', category: '生活', difficulty: 1, state: '草稿', version: 1, aliases: [], hints: ['需要风力', '通常有线牵引'], explanation: '借风力在空中飞行的玩具。', calibration: '未评测', source: '合成草稿' },
    { id: 'Q-042', word: ' bridge', category: '事物', difficulty: 2, state: '待质检', version: 1, aliases: [], hints: [], explanation: '', calibration: '未评测', source: '合成坏行示例' },
  ],
  importReport: {
    file: 'synthetic-wordbank.csv', total: 100, valid: 96, duplicate: 2,
    errors: [{ row: 18, column: 'hints', message: '提示超过 120 字' }, { row: 42, column: 'word', message: '答案须为 2–4 个汉字' }],
  },
};

export const scoringFixture: ScoringView = {
  synthetic: true,
  model: 'BAAI/bge-small-zh-v1.5',
  revision: 'demo-revision-not-frozen',
  calibration: 'cal-v3（合成）',
  wordbankVersion: 'lex-v1（合成）',
  blindLocked: false,
  results: [
    { guess: '橘子', raw: 0.731, display: 82.4, rating: 'hot', exact: false },
    { guess: '雪梨', raw: 0.702, display: 79.6, rating: 'hot', exact: false },
    { guess: '果园', raw: 0.631, display: 69.5, rating: 'warm', exact: false },
    { guess: '铁路', raw: 0.162, display: 18.2, rating: 'cold', exact: false },
  ],
};

export const diagnosticsFixture: DiagnosticsView = {
  synthetic: true,
  messages: [
    { id: 'msg_demo_001', word: '橘子', reason: 'SCORED', status: '已处理', receivedAt: '19:41:08.120' },
    { id: 'msg_demo_002', word: '果汁', reason: 'SCORED', status: '已处理', receivedAt: '19:41:07.982' },
    { id: 'msg_demo_003', word: '橘子', reason: 'DUPLICATE', status: '已去重', receivedAt: '19:41:06.414' },
  ],
  trace: [
    { stage: '中继持久接收', status: 'done', time: '19:41:08.100', evidence: 'relay_seq 合成 1024' },
    { stage: '本机 persisted', status: 'done', time: '19:41:08.108', evidence: 'ACK 合成记录' },
    { stage: '入场与评分', status: 'done', time: '19:41:08.116', evidence: 'reason SCORED' },
    { stage: 'display received', status: 'done', time: '19:41:08.120', evidence: 'delivery_demo_001' },
    { stage: 'display processed', status: 'pending', time: '—', evidence: '等待本次真实渲染；当前仅演练' },
    { stage: '平台履约', status: 'unknown', time: '—', evidence: '尚无真实平台证据' },
  ],
  replay: { id: 'replay_demo_01', status: 'paused', done: 42, total: 100, targetMode: 'replay' },
};

export const analyticsFixture: AnalyticsView = {
  synthetic: true,
  runPurpose: 'local',
  sampledAt: '2026-10-25 19:41:08',
  pendingJobs: 2,
  metrics: [
    { label: '有效参与人数', value: '38', note: '独立脱敏参与者' },
    { label: '有效独立猜词', value: '126', note: '不含格式错误与重复' },
    { label: '每分钟参与', value: '18.4', note: '当前合成时间窗' },
    { label: '每轮完成率', value: '71.4%', note: 'SOLVED / 已终结轮' },
    { label: '延迟 p95', value: '420 ms', note: '本机接收到展示处理' },
    { label: '异常轮', value: '1', note: 'INTERRUPTED 合成样例' },
  ],
  trend: [{ label: '19:35', value: 12 }, { label: '19:36', value: 20 }, { label: '19:37', value: 16 }, { label: '19:38', value: 28 }, { label: '19:39', value: 24 }, { label: '19:40', value: 36 }, { label: '19:41', value: 30 }],
  rounds: [
    { id: 'round_demo_7', reason: 'RUNNING', duration: '02:18', hints: 2, status: '暂计，待收尾' },
    { id: 'round_demo_6', reason: 'SOLVED', duration: '03:42', hints: 1, status: '已结算' },
    { id: 'round_demo_5', reason: 'TIMEOUT', duration: '04:00', hints: 3, status: '已结算' },
    { id: 'round_demo_4', reason: 'INTERRUPTED', duration: '01:16', hints: 0, status: '故障中断' },
  ],
};

export const settingsFixture: SettingsView = {
  synthetic: true,
  rules: { duration: 240, intermission: 4, autoNext: true, difficulty: [1, 5], strictCode: true },
  model: { id: 'bge-small-zh-v1.5', revision: 'demo-not-frozen', calibration: 'cal-v3', status: '合成预热完成' },
  backup: { lastSuccess: '2026-10-25 18:30', version: 'schema v1 · demo', status: '仅有合成记录，未执行真实备份' },
  display: { connected: true, generation: '12', expiresAt: '2026-10-26 03:41' },
  activeSession: null,
};
