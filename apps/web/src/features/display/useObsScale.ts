import { useEffect, useState } from 'react';

export type ObsLayout = 'portrait' | 'landscape';

export function obsDesignSize(layout: ObsLayout): { width: number; height: number } {
  return layout === 'portrait' ? { width: 1080, height: 1920 } : { width: 1920, height: 1080 };
}

export function calculateObsScale(layout: ObsLayout, viewportWidth: number, viewportHeight: number): number {
  const design = obsDesignSize(layout);
  return Math.max(0.01, Math.min(viewportWidth / design.width, viewportHeight / design.height));
}

export function useObsScale(layout: ObsLayout): number {
  const [scale, setScale] = useState(() => calculateObsScale(layout, window.innerWidth, window.innerHeight));
  useEffect(() => {
    const update = () => setScale(calculateObsScale(layout, window.innerWidth, window.innerHeight));
    update();
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, [layout]);
  return scale;
}
