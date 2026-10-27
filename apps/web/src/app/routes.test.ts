import { describe, expect, it } from 'vitest';
import { appRoutes } from './routes';

describe('应用路由', () => {
  it('覆盖七类私有页面和独立 OBS 页面', () => {
    expect(appRoutes).toEqual([
      '/setup',
      '/studio/:sessionId',
      '/words',
      '/scoring',
      '/replay',
      '/analytics/:sessionId',
      '/settings',
      '/obs/:sessionId',
    ]);
  });
});
