import { describe, expect, it } from 'vitest';
import { fixtureAdapter } from './adapter';

describe('合成数据端口', () => {
  it('公开视图不包含谜底或平台敏感字段', async () => {
    const display = await fixtureAdapter.getPublicDisplay();
    const serialized = JSON.stringify(display);
    expect(display.synthetic).toBe(true);
    expect(serialized).not.toContain('"answer"');
    expect(serialized).not.toContain('aliases');
    expect(serialized).not.toContain('target_vector');
    expect(serialized).not.toContain('platform_uid');
  });

  it('演练命令只返回本地演练结果', async () => {
    const result = await fixtureAdapter.executeDemoCommand('pause');
    expect(result).toEqual({
      status: 'applied',
      message: '本地演练已应用：pause',
      synthetic: true,
    });
  });

  it('真实能力明确保持未接入', async () => {
    const result = await fixtureAdapter.requestRealCapability();
    expect(result.status).toBe('unavailable');
    expect(result.message).toBe('待真实 API 接入');
  });
});
