import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { AppRoutes } from './App';

function renderRoute(route: string) {
  return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={[route]}><AppRoutes /></MemoryRouter></QueryClientProvider>);
}

describe('应用路由', () => {
  it('进入准备页并显示合成演练边界', async () => {
    renderRoute('/setup');
    expect(await screen.findByRole('heading', { name: '开播准备' })).toBeInTheDocument();
    expect(screen.getByText(/全部页面使用合成 fixture/)).toBeInTheDocument();
  });

  it('公开 OBS 路由不渲染后台导航或私有谜底', async () => {
    renderRoute('/obs/session_demo');
    expect(await screen.findByText(/发送「猜 A7 苹果」/)).toBeInTheDocument();
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
    expect(screen.queryByText('柠檬')).not.toBeInTheDocument();
  });

  it('场次复盘路由使用路径中的 sessionId', async () => {
    renderRoute('/analytics/session_from_route');
    expect(await screen.findByRole('heading', { name: '场次复盘' })).toBeInTheDocument();
    expect(screen.getByText(/session_from_route · 统计时点/)).toBeInTheDocument();
  });
});
