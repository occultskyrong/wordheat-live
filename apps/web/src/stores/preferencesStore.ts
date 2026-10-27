export const PREFERENCES_KEY = 'wordheat-display-preferences:v0.1.0';

export type DisplayPreferences = {
  layout: 'portrait' | 'landscape';
  fontScale: 0.9 | 1 | 1.1 | 1.2;
  safeArea: boolean;
  reducedMotion: boolean;
};

export const defaultDisplayPreferences: DisplayPreferences = {
  layout: 'portrait',
  fontScale: 1,
  safeArea: true,
  reducedMotion: false,
};

const fontScales: DisplayPreferences['fontScale'][] = [0.9, 1, 1.1, 1.2];

export function loadDisplayPreferences(): DisplayPreferences {
  try {
    const raw = localStorage.getItem(PREFERENCES_KEY);
    if (!raw) return { ...defaultDisplayPreferences };
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return { ...defaultDisplayPreferences };
    const candidate = value as Record<string, unknown>;
    if ((candidate.layout !== 'portrait' && candidate.layout !== 'landscape')
      || !fontScales.includes(candidate.fontScale as DisplayPreferences['fontScale'])
      || typeof candidate.safeArea !== 'boolean'
      || typeof candidate.reducedMotion !== 'boolean') {
      return { ...defaultDisplayPreferences };
    }
    return {
      layout: candidate.layout,
      fontScale: candidate.fontScale as DisplayPreferences['fontScale'],
      safeArea: candidate.safeArea,
      reducedMotion: candidate.reducedMotion,
    };
  } catch {
    return { ...defaultDisplayPreferences };
  }
}

export function saveDisplayPreferences(preferences: DisplayPreferences): void {
  const value: DisplayPreferences = {
    layout: preferences.layout,
    fontScale: preferences.fontScale,
    safeArea: preferences.safeArea,
    reducedMotion: preferences.reducedMotion,
  };
  localStorage.setItem(PREFERENCES_KEY, JSON.stringify(value));
}
