import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { displayFixture } from '../../api/fixture/data';
import { ObsPage } from '../../pages/obs/ObsPage';

 describe('OBS 双设计画布', () => {
  it('横屏和竖屏暴露固定设计尺寸与等比缩放容器', () => {
    const landscape = render(<ObsPage initialData={displayFixture} {...({ layout: 'landscape' } as any)} />);
    expect(landscape.container.querySelector('.obs-canvas')).toHaveAttribute('data-design', '1920x1080');
    expect(landscape.container.querySelector('.obs-canvas')).toHaveStyle({ transform: 'scale(0.533333)' });
    landscape.unmount();
    const portrait = render(<ObsPage initialData={displayFixture} {...({ layout: 'portrait' } as any)} />);
    expect(portrait.container.querySelector('.obs-canvas')).toHaveAttribute('data-design', '1080x1920');
    expect(portrait.container.querySelector('.obs-stage')).toHaveStyle({ placeItems: 'center' });
  });
});
