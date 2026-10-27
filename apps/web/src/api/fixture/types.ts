export type Synthetic = { synthetic: true };
export type RoundStatus = 'IDLE' | 'PREPARING' | 'RUNNING' | 'PAUSED' | 'DRAINING' | 'REVEALED' | 'INTERMISSION' | 'FAULTED';
export type TransportStatus = 'DISCONNECTED' | 'CONNECTING' | 'CONNECTED' | 'RECONNECTING' | 'FAULTED';
export type RunPurpose = 'local' | 'test_live' | 'production';

export interface PublicClock {
  timeOrigin: 'relay' | 'local_rehearsal' | 'local_replay';
  deadlineAt: string | null;
  remainingMs: number;
}

export interface RoundView {
  id: string;
  code: string;
  category: string;
  answerLength: number;
  status: RoundStatus;
  remainingSeconds: number;
  version: number;
  clock: PublicClock;
  reason?: string;
  nextStartAt?: string | null;
}

export interface FeedbackView {
  id: string;
  word: string | null;
  name: string;
  score: number | null;
  status: 'scored' | 'format_invalid' | 'duplicate' | 'rate_limited' | 'round_closed' | 'system_failed';
  solved: boolean;
  message: string;
}

export interface DisplayView extends Synthetic {
  sessionId: string;
  mode: 'rehearsal' | 'official' | 'replay';
  runPurpose: RunPurpose;
  serverTime: string;
  asOfSeq: string;
  authorization: 'AUTHORIZED' | 'UNAUTHORIZED';
  purpose: 'primary' | 'preview';
  renderGeneration: string;
  round: RoundView | null;
  transport: TransportStatus;
  hints: string[];
  latest: FeedbackView | null;
  feedback: FeedbackView[];
  wordRanking: Array<{ word: string; name: string; count: number; score: number }>;
  playerRanking: Array<{ id: string; name: string; points: number }>;
  reveal: null | { word: string; explanation: string; winner: string | null; reason: string };
  authorizedDeliveryIds: string[];
}

export interface ReadinessView extends Synthetic {
  checks: Array<{ name: string; status: 'pass' | 'fail' | 'pending'; detail: string }>;
  gates: Array<{ name: string; status: 'pass' | 'fail' | 'pending'; detail: string }>;
  capabilities: { rehearse: boolean; testLive: boolean; goLive: boolean };
  runtime: { launcher: string; model: string; schema: string; sampledAt: string };
}

export interface StudioView extends Synthetic {
  sessionId: string;
  privateAnswer: string;
  nextHint: string | null;
  display: DisplayView;
  readiness: ReadinessView;
  queue: { received: number; scored: number; displayed: number; pending: number };
}

export interface PuzzleView {
  id: string;
  word: string;
  category: string;
  difficulty: number;
  state: '草稿' | '待质检' | '待审核' | '已发布' | '已禁用';
  version: number;
  aliases: string[];
  hints: string[];
  explanation: string;
  calibration: string;
  source: string;
}

export interface WordbankView extends Synthetic {
  puzzles: PuzzleView[];
  importReport: { file: string; total: number; valid: number; duplicate: number; errors: Array<{ row: number; column: string; message: string }> };
}

export interface ScoringView extends Synthetic {
  model: string;
  revision: string;
  calibration: string;
  wordbankVersion: string;
  blindLocked: boolean;
  results: Array<{ guess: string; raw: number; display: number; rating: string; exact: boolean }>;
}

export interface DiagnosticsView extends Synthetic {
  messages: Array<{ id: string; word: string; reason: string; status: string; receivedAt: string }>;
  trace: Array<{ stage: string; status: 'done' | 'pending' | 'failed' | 'unknown'; time: string; evidence: string }>;
  replay: { id: string; status: string; done: number; total: number; targetMode: 'replay' };
}

export interface AnalyticsView extends Synthetic {
  runPurpose: RunPurpose;
  sampledAt: string;
  pendingJobs: number;
  metrics: Array<{ label: string; value: string; note: string }>;
  trend: Array<{ label: string; value: number }>;
  rounds: Array<{ id: string; reason: string; duration: string; hints: number; status: string }>;
}

export interface SettingsView extends Synthetic {
  rules: { duration: number; intermission: number; autoNext: boolean; difficulty: [number, number]; strictCode: boolean };
  model: { id: string; revision: string; calibration: string; status: string };
  backup: { lastSuccess: string; version: string; status: string };
  display: { connected: boolean; generation: string; expiresAt: string };
  activeSession: { id: string; status: string } | null;
}

export interface DemoResult {
  status: 'applied' | 'unavailable';
  message: string;
  synthetic: true;
}
