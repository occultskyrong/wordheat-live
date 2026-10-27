import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useLocation } from 'react-router-dom';
import { readFixtureScenario, type FixtureScenario } from '../api/fixture/scenarios';
import { AsyncPanel, InlineNotice } from '../components/ui';

type Props<T> = {
  queryKey: string;
  queryFn: () => Promise<T>;
  children: (data: T, context: { stale: boolean }) => ReactNode;
  scenario?: FixtureScenario;
};

export function FixtureQuery<T>({ queryKey, queryFn, children, scenario: forcedScenario }: Props<T>) {
  const location = useLocation();
  const scenario = forcedScenario ?? readFixtureScenario(location.search);
  const query = useQuery({ queryKey: ['fixture', queryKey], queryFn, staleTime: Infinity });

  if (scenario === 'loading') return <div className="async-panel" role="status" aria-busy="true"><span className="loading-line" />正在加载 {queryKey}…</div>;
  if (scenario === 'forbidden') return <AsyncPanel loading={false} error="当前身份无权访问此视图，请切换有权限的身份后重试" empty={false}>{null}</AsyncPanel>;
  if (scenario === 'contract-error') return <AsyncPanel loading={false} error="前后端契约版本不匹配，请更新客户端并重新读取" empty={false}>{null}</AsyncPanel>;
  if (scenario === 'empty') return <AsyncPanel loading={false} error={null} empty emptyText="当前筛选下暂无数据，请返回开播准备或调整筛选">{null}</AsyncPanel>;

  return <>
    {scenario === 'partial-error' && <div role="alert"><InlineNotice tone="warn">部分数据更新失败，当前保留的是过期内容 · 合成采样时点；请重连后核对最新状态。</InlineNotice></div>}
    <AsyncPanel loading={query.isPending} error={query.error?.message ?? null} empty={false}>
      {query.data ? children(query.data, { stale: scenario === 'partial-error' }) : null}
    </AsyncPanel>
  </>;
}
