import type { DisplayView } from '../../api/fixture/types';

export interface SyncState {
  lastSeq: string;
  live: boolean;
  receiptsEnabled: boolean;
  view: DisplayView;
  error: string | null;
}

export function compareSeq(a: string, b: string): -1 | 0 | 1 {
  if (!/^\d+$/.test(a) || !/^\d+$/.test(b)) throw new Error('INVALID_SEQUENCE');
  const left = BigInt(a);
  const right = BigInt(b);
  return left < right ? -1 : left > right ? 1 : 0;
}

export function createSyncState(view: DisplayView, asOfSeq: string): SyncState {
  compareSeq(asOfSeq, asOfSeq);
  return { lastSeq: asOfSeq, live: false, receiptsEnabled: false, view, error: null };
}

export function reduceDisplayEvent(state: SyncState, event: { seq: string; view: DisplayView }): SyncState {
  const comparison = compareSeq(event.seq, state.lastSeq);
  if (comparison < 0) return state;
  if (comparison === 0) {
    return JSON.stringify(event.view) === JSON.stringify(state.view) ? state : { ...state, live: false, receiptsEnabled: false, error: 'SEQUENCE_CONTENT_CONFLICT' };
  }
  if (compareSeq(event.seq, (BigInt(state.lastSeq) + 1n).toString()) !== 0) {
    return { ...state, live: false, receiptsEnabled: false, error: 'RESYNC_REQUIRED' };
  }
  return { lastSeq: event.seq, live: state.live, receiptsEnabled: state.receiptsEnabled, view: event.view, error: null };
}

export function authorizeResume(state: SyncState, uptoSeq: string): SyncState {
  if (compareSeq(uptoSeq, state.lastSeq) !== 0) throw new Error('RESUME_BOUNDARY_MISMATCH');
  return { ...state, live: true, receiptsEnabled: true, error: null };
}
