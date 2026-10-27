import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { DemoResult } from '../../api/fixture/types';
import { scoringFixture } from '../../api/fixture/data';
import { ScoringPage } from './ScoringPage';

describe('评分实验室', () => {
  it('原始余弦与展示分分列，不混淆为概率', () => {
    render(<ScoringPage initialData={{ ...scoringFixture, blindLocked: true }} />);
    expect(screen.getByRole('columnheader', { name: '原始余弦' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '展示分' })).toBeInTheDocument();
    expect(screen.getByText('0.731')).toBeInTheDocument();
    expect(screen.getByText('82.4')).toBeInTheDocument();
    expect(screen.getByText('demo-revision-not-frozen')).toBeInTheDocument();
    expect(screen.getByText(/非概率/)).toBeInTheDocument();
  });

  it('blind 集在人工标注锁定前隐藏全部分数', () => {
    const data = { ...scoringFixture, blindLocked: false };
    render(<ScoringPage initialData={data} />);
    expect(screen.getByLabelText('数据 split')).toHaveValue('blind');
    expect(screen.getByText('盲标未锁定，模型分数已隐藏')).toBeInTheDocument();
    expect(screen.queryByText('0.731')).not.toBeInTheDocument();
    expect(screen.queryByText('82.4')).not.toBeInTheDocument();
  });

  it('锁定 blind 标注后才显示结果，前端没有 pass=true 捷径', async () => {
    const user = userEvent.setup();
    render(<ScoringPage initialData={{ ...scoringFixture, blindLocked: true }} />);
    expect(screen.getByText('0.731')).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('数据 split'), 'explore');
    expect(screen.getByText('0.731')).toBeInTheDocument();
    expect(screen.queryByText('pass=true')).not.toBeInTheDocument();
  });

  it('评测请求失败后恢复提交并明确未生成真实分数', async () => {
    const user = userEvent.setup();
    let reject!: (error: Error) => void;
    const onCommand = vi.fn(() => new Promise<DemoResult>((_, rejectPromise) => { reject = rejectPromise; }));
    render(<ScoringPage initialData={scoringFixture} onCommand={onCommand} />);
    await user.click(screen.getByRole('button', { name: '提交合成评测任务' }));
    expect(screen.getByRole('button', { name: '评测提交中…' })).toBeDisabled();
    reject(new Error('模型暂不可用'));
    expect(await screen.findByText(/模型暂不可用 · 未生成真实分数/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '提交合成评测任务' })).toBeEnabled();
  });
});
