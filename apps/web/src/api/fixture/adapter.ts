import { analyticsFixture, diagnosticsFixture, displayFixture, readinessFixture, scoringFixture, settingsFixture, studioFixture, wordbankFixture } from './data';
import type { DemoResult } from './types';

const resolve = <T,>(value: T): Promise<T> => Promise.resolve(structuredClone(value));

export const fixtureAdapter = {
  getSetup: () => resolve(readinessFixture),
  getStudio: () => resolve(studioFixture),
  getPublicDisplay: () => resolve(displayFixture),
  getWords: () => resolve(wordbankFixture),
  getScoring: () => resolve(scoringFixture),
  getDiagnostics: () => resolve(diagnosticsFixture),
  getAnalytics: () => resolve(analyticsFixture),
  getSettings: () => resolve(settingsFixture),
  executeDemoCommand: async (command: string): Promise<DemoResult> => ({
    status: 'applied',
    message: `本地演练已应用：${command}`,
    synthetic: true,
  }),
  requestRealCapability: async (): Promise<DemoResult> => ({
    status: 'unavailable',
    message: '待真实 API 接入',
    synthetic: true,
  }),
};
