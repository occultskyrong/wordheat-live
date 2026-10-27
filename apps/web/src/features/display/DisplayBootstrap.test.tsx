import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { displayFixture } from '../../api/fixture/data';
import type { DisplayView } from '../../api/fixture/types';
import { PREFERENCES_KEY } from '../../stores/preferencesStore';
import { DisplayBootstrap } from './DisplayBootstrap';

function view(overrides: Partial<DisplayView> = {}): DisplayView {
  return { ...displayFixture, ...overrides };
}

describe('合成 DISPLAY bootstrap 与真实渲染回执', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
  });

  it('primary 在可见 DOM 提交后依次记录 received 和 processed', async () => {
    const onReceipt = vi.fn();
    render(<DisplayBootstrap initialData={view({ purpose: 'primary' })} onReceipt={onReceipt} />);
    expect(await screen.findByText('橘子', { selector: '.obs-feedback-word' })).toBeInTheDocument();
    await waitFor(() => expect(onReceipt).toHaveBeenCalledWith('received', 'delivery_demo_1'));
    await waitFor(() => expect(onReceipt).toHaveBeenCalledWith('processed', 'delivery_demo_1'));
  });

  it('preview 不产生任何履约回执', async () => {
    const onReceipt = vi.fn();
    render(<DisplayBootstrap initialData={view({ purpose: 'preview' })} onReceipt={onReceipt} />);
    expect(await screen.findByText('橘子', { selector: '.obs-feedback-word' })).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector('[data-feedback-id="feedback_demo_1"]')).not.toBeNull());
    expect(onReceipt).not.toHaveBeenCalled();
  });

  it('preview 应用本机布局、字号、安全区和减少动画偏好，primary 不读取偏好', () => {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify({ layout: 'landscape', fontScale: 1.2, safeArea: true, reducedMotion: true }));
    const { unmount } = render(<DisplayBootstrap initialData={view({ purpose: 'preview' })} />);
    expect(document.querySelector('.obs-stage')).toHaveAttribute('data-layout', 'landscape');
    expect(document.querySelector('.obs-stage')).toHaveAttribute('data-font-scale', '1.2');
    expect(document.querySelector('.obs-stage')).toHaveAttribute('data-reduced-motion', 'true');
    expect(document.querySelector('[data-design="1920x1080"]')).toBeInTheDocument();
    expect(screen.getByLabelText('预览安全区')).toBeInTheDocument();
    unmount();
    render(<DisplayBootstrap initialData={view({ purpose: 'primary' })} />);
    expect(document.querySelector('.obs-stage')).toHaveAttribute('data-layout', 'landscape');
    expect(document.querySelector('.obs-stage')).toHaveAttribute('data-font-scale', '1');
    expect(document.querySelector('.obs-stage')).toHaveAttribute('data-reduced-motion', 'false');
    expect(screen.queryByLabelText('预览安全区')).not.toBeInTheDocument();
  });

  it('隐藏文档可以 received 但不能 processed', async () => {
    const onReceipt = vi.fn();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    render(<DisplayBootstrap initialData={view({ purpose: 'primary' })} onReceipt={onReceipt} />);
    await waitFor(() => expect(onReceipt).toHaveBeenCalledWith('received', 'delivery_demo_1'));
    expect(onReceipt).not.toHaveBeenCalledWith('processed', 'delivery_demo_1');
    visibility.mockRestore();
  });

  it('序列缺口停止 ready 和回执并显示重同步', async () => {
    const onReceipt = vi.fn();
    const initial = view({ purpose: 'primary', asOfSeq: '8' });
    const events = [{ seq: '10', view: view({ purpose: 'primary', asOfSeq: '10' }) }];
    render(<DisplayBootstrap initialData={initial} events={events} onReceipt={onReceipt} />);
    expect(await screen.findByText('展示数据需要重新同步')).toBeInTheDocument();
    expect(onReceipt).not.toHaveBeenCalled();
  });

  it('resume_ready 前只恢复投影而不产生回执', async () => {
    const onReceipt = vi.fn();
    const next = view({ purpose: 'primary', asOfSeq: '9007199254740994', hints: ['恢复后的公开提示'] });
    render(<DisplayBootstrap initialData={view({ purpose: 'primary' })} events={[{ seq: '9007199254740994', view: next }]} ready={false} onReceipt={onReceipt} />);
    expect(await screen.findByText(/恢复后的公开提示/)).toBeInTheDocument();
    expect(onReceipt).not.toHaveBeenCalled();
  });
});
