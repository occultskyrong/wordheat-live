import { describe, expect, it } from 'vitest';
import { displayFixture } from '../../api/fixture/data';
import { authorizeResume, compareSeq, createSyncState, reduceDisplayEvent } from './syncMachine';

describe('DISPLAY 同步状态机', () => {
  it('用 BigInt 比较超过 JS 安全整数的序号', () => {
    expect(compareSeq('9007199254740993', '9007199254740992')).toBe(1);
    expect(compareSeq('10', '2')).toBe(1);
  });

  it('重复事件不重复修改绝对值投影', () => {
    const initial = createSyncState(displayFixture, '9007199254740993');
    const nextView = { ...displayFixture, playerRanking: [{ id: 'participant_demo_1', name: '小林', points: 120 }] };
    const next = reduceDisplayEvent(initial, { seq: '9007199254740994', view: nextView });
    const duplicate = reduceDisplayEvent(next, { seq: '9007199254740994', view: nextView });
    expect(duplicate.view.playerRanking[0].points).toBe(120);
    expect(duplicate).toBe(next);
  });

  it('缺口显式进入重同步且不跳游标', () => {
    const initial = createSyncState(displayFixture, '8');
    const result = reduceDisplayEvent(initial, { seq: '10', view: displayFixture });
    expect(result.lastSeq).toBe('8');
    expect(result.error).toBe('RESYNC_REQUIRED');
    expect(result.live).toBe(false);
  });

  it('resume_ready 前禁止回执，边界一致后才授权', () => {
    const initial = createSyncState(displayFixture, '12');
    expect(initial.receiptsEnabled).toBe(false);
    const ready = authorizeResume(initial, '12');
    expect(ready.live).toBe(true);
    expect(ready.receiptsEnabled).toBe(true);
    expect(() => authorizeResume(initial, '13')).toThrow('RESUME_BOUNDARY_MISMATCH');
  });
});
