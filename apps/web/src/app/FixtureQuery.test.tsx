import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { readFixtureScenario } from '../api/fixture/scenarios';
import { FixtureQuery } from './FixtureQuery';

function renderScenario(scenario: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/words?scenario=${scenario}`]}>
        <FixtureQuery queryKey="words" queryFn={async () => ({ word: '橘子' })}>
          {(data, { stale }) => <button disabled={stale}>处理 {data.word}</button>}
        </FixtureQuery>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('共享合成场景容器', () => {
  it('只接受白名单场景，未知值回退 normal', () => {
    expect(readFixtureScenario('?scenario=partial-error')).toBe('partial-error');
    expect(readFixtureScenario('?scenario=unexpected')).toBe('normal');
  });

  it('loading 显示带查询上下文的首次加载态', () => {
    renderScenario('loading');
    expect(screen.getByRole('status')).toHaveTextContent('正在加载 words');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('empty 替换内容并给出下一步', async () => {
    renderScenario('empty');
    expect(await screen.findByText('当前筛选下暂无数据，请返回开播准备或调整筛选')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('normal 向页面提供非过期上下文', async () => {
    renderScenario('normal');
    expect(await screen.findByRole('button', { name: '处理 橘子' })).toBeEnabled();
  });

  it('partial-error 保留旧内容并标记过期采样', async () => {
    renderScenario('partial-error');
    expect(await screen.findByRole('button', { name: '处理 橘子' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('部分数据更新失败');
    expect(screen.getByRole('alert')).toHaveTextContent('过期');
    expect(screen.getByRole('alert')).toHaveTextContent('合成采样时点');
  });

  it('forbidden 替换依赖权限的内容和动作', () => {
    renderScenario('forbidden');
    expect(screen.getByRole('alert')).toHaveTextContent('当前身份无权访问此视图');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('contract-error 明确契约版本不匹配', () => {
    renderScenario('contract-error');
    expect(screen.getByRole('alert')).toHaveTextContent('前后端契约版本不匹配');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
