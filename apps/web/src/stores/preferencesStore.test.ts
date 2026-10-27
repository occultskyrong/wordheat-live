import { beforeEach, describe, expect, it } from 'vitest';
import { defaultDisplayPreferences, loadDisplayPreferences, PREFERENCES_KEY, saveDisplayPreferences } from './preferencesStore';

describe('版本化 OBS 预览偏好', () => {
  beforeEach(() => localStorage.clear());

  it('保存并恢复完整无秘密偏好', () => {
    const value = { layout: 'landscape', fontScale: 1.2, safeArea: false, reducedMotion: true } as const;
    saveDisplayPreferences(value);
    expect(PREFERENCES_KEY).toBe('wordheat-display-preferences:v0.1.0');
    expect(loadDisplayPreferences()).toEqual(value);
    expect(localStorage.getItem(PREFERENCES_KEY)).not.toContain('ticket');
  });

  it.each([
    '{bad json',
    JSON.stringify({ layout: 'diagonal', fontScale: 1, safeArea: true, reducedMotion: false }),
    JSON.stringify({ layout: 'portrait', fontScale: 4, safeArea: true, reducedMotion: false }),
  ])('坏 JSON 或非法字段回退设计默认', (stored) => {
    localStorage.setItem(PREFERENCES_KEY, stored);
    expect(loadDisplayPreferences()).toEqual(defaultDisplayPreferences);
  });

  it('恢复默认也立即持久化', () => {
    saveDisplayPreferences({ layout: 'landscape', fontScale: 1.1, safeArea: false, reducedMotion: true });
    saveDisplayPreferences(defaultDisplayPreferences);
    expect(loadDisplayPreferences()).toEqual({ layout: 'portrait', fontScale: 1, safeArea: true, reducedMotion: false });
  });
});
