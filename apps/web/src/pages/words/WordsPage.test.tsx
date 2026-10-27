import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { wordbankFixture } from '../../api/fixture/data';
import type { PuzzleView } from '../../api/fixture/types';
import { WordsPage } from './WordsPage';

afterEach(() => vi.useRealTimers());

describe('题库管理', () => {
  it('已发布版本只读，草稿可以编辑并保留版本', async () => {
    const user = userEvent.setup();
    render(<WordsPage initialData={wordbankFixture} />);
    await user.click(screen.getByRole('button', { name: /Q-018/ }));
    expect(screen.getByLabelText('答案')).toHaveValue('柠檬');
    expect(screen.getByLabelText('答案')).toHaveAttribute('readonly');
    expect(screen.getByText('已发布版本只读；修订必须创建新草稿')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Q-031/ }));
    expect(screen.getByLabelText('答案')).not.toHaveAttribute('readonly');
    expect(screen.getByText('编辑版本 v1 · 保存附带 expected_version')).toBeInTheDocument();
  });

  it('展示服务端 CSV 行列错误且不把有效行当已发布', () => {
    render(<WordsPage initialData={wordbankFixture} />);
    expect(screen.getByText('synthetic-wordbank.csv')).toBeInTheDocument();
    expect(screen.getByText('第 18 行 · hints')).toBeInTheDocument();
    expect(screen.getByText('提示超过 120 字')).toBeInTheDocument();
    expect(screen.getByText('第 42 行 · word')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认 96 条有效行（合成）' })).toBeInTheDocument();
    expect(screen.getByText(/确认前不会写入或发布/)).toBeInTheDocument();
  });

  it('搜索去抖 300ms，分类和难度参与筛选', async () => {
    vi.useFakeTimers();
    render(<WordsPage initialData={wordbankFixture} />);
    fireEvent.change(screen.getByLabelText('搜索题目'), { target: { value: '萤火' } });
    expect(screen.getByRole('button', { name: /Q-018/ })).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(299));
    expect(screen.getByRole('button', { name: /Q-018/ })).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByRole('button', { name: /Q-018/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Q-024/ })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('搜索题目'), { target: { value: '' } });
    act(() => vi.advanceTimersByTime(300));
    fireEvent.change(screen.getByLabelText('分类'), { target: { value: '自然' } });
    fireEvent.change(screen.getByLabelText('难度'), { target: { value: '3' } });
    expect(screen.getByRole('button', { name: /Q-024/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Q-031/ })).not.toBeInTheDocument();
  });

  it('每页 20 条，筛选变化重置页码', async () => {
    const user = userEvent.setup();
    const puzzles: PuzzleView[] = Array.from({ length: 25 }, (_, index) => ({ ...wordbankFixture.puzzles[2], id: `Q-page-${index + 1}`, word: `题目${index + 1}` }));
    render(<WordsPage initialData={{ ...wordbankFixture, puzzles }} />);
    expect(screen.getAllByRole('button', { name: /Q-page-/ })).toHaveLength(20);
    await user.click(screen.getByRole('button', { name: '下一页' }));
    expect(screen.getAllByRole('button', { name: /Q-page-/ })).toHaveLength(5);
    await user.selectOptions(screen.getByLabelText('难度'), '1');
    expect(screen.getAllByRole('button', { name: /Q-page-/ })).toHaveLength(20);
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled();
  });

  it('筛选无结果提供清除筛选', async () => {
    const user = userEvent.setup();
    render(<WordsPage initialData={wordbankFixture} />);
    await user.type(screen.getByLabelText('搜索题目'), '不存在');
    expect(await screen.findByText('筛选无结果')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '清除筛选' }));
    expect(screen.getByRole('button', { name: /Q-018/ })).toBeInTheDocument();
  });

  it('草稿完整字段可编辑，切换题目需确认继续或放弃', async () => {
    const user = userEvent.setup();
    render(<WordsPage initialData={wordbankFixture} />);
    await user.click(screen.getByRole('button', { name: /Q-031/ }));
    await user.clear(screen.getByLabelText('答案'));
    await user.type(screen.getByLabelText('答案'), '纸鸢');
    await user.clear(screen.getByLabelText('题目分类'));
    await user.type(screen.getByLabelText('题目分类'), '玩具');
    await user.clear(screen.getByLabelText('题目难度'));
    await user.type(screen.getByLabelText('题目难度'), '2');
    await user.type(screen.getByLabelText('审核别名'), '风筝玩具');
    await user.type(screen.getByLabelText('解释'), '借风力飞行的玩具');
    await user.click(screen.getByRole('button', { name: /Q-018/ }));
    expect(screen.getByRole('dialog')).toHaveTextContent('未保存草稿');
    await user.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(screen.getByLabelText('答案')).toHaveValue('纸鸢');
    await user.click(screen.getByRole('button', { name: /Q-018/ }));
    await user.click(screen.getByRole('button', { name: '放弃草稿' }));
    expect(screen.getByLabelText('答案')).toHaveValue('柠檬');
  });

  it('保存冲突保留草稿，离页触发未保存保护', async () => {
    const user = userEvent.setup();
    const onCommand = vi.fn(async () => { throw new Error('VERSION_CONFLICT'); });
    render(<WordsPage initialData={wordbankFixture} onCommand={onCommand} />);
    await user.click(screen.getByRole('button', { name: /Q-031/ }));
    await user.clear(screen.getByLabelText('答案'));
    await user.type(screen.getByLabelText('答案'), '纸鸢');
    const event = new Event('beforeunload', { cancelable: true });
    expect(window.dispatchEvent(event)).toBe(false);
    await user.click(screen.getByRole('button', { name: '保存草稿' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('草稿已保留');
    expect(screen.getByLabelText('答案')).toHaveValue('纸鸢');
  });

  it('过期题库保留可读内容但禁用所有写入口', () => {
    render(<WordsPage initialData={wordbankFixture} stale />);
    expect(screen.getByText(/题库数据已过期/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '新建草稿' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '提交质检' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '选择 CSV' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '确认 96 条有效行（合成）' })).toBeDisabled();
  });
});
