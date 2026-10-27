import { describe, expect, it } from 'vitest';
import { deriveConnectionStatus } from './status';

describe('连接与开播许可', () => {
  it('短重连保持回合计时，不显示手动暂停', () => {
    const state = deriveConnectionStatus({ transport: 'RECONNECTING', leaseValid: true, hasUnrecoverableGap: false, runPurpose: 'local' });
    expect(state.clockContinues).toBe(true);
    expect(state.leaseCommandsBlocked).toBe(true);
    expect(state.label).toContain('时间继续');
    expect(state.label).not.toContain('暂停');
  });

  it.each([{ leaseValid: false, hasUnrecoverableGap: false }, { leaseValid: true, hasUnrecoverableGap: true }])('租约失效或不可补齐 gap 禁止开播', (flags) => {
    const state = deriveConnectionStatus({ transport: 'CONNECTED', runPurpose: 'production', ...flags });
    expect(state.canStart).toBe(false);
  });

  it('平台测试不是正式生产来源', () => {
    const state = deriveConnectionStatus({ transport: 'CONNECTED', leaseValid: true, hasUnrecoverableGap: false, runPurpose: 'test_live' });
    expect(state.canStart).toBe(true);
    expect(state.isProduction).toBe(false);
  });
});
