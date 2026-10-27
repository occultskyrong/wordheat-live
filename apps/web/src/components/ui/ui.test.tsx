import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { AsyncPanel, ConfirmDialog, ModeBadge, TextCell } from './index';

describe('公共 UI', () => {
  it('异步容器显示可读加载状态', () => {
    render(<AsyncPanel loading error={null} empty={false}>内容</AsyncPanel>);
    expect(screen.getByRole('status')).toHaveTextContent('正在加载');
  });

  it('异步容器保留具体错误', () => {
    render(<AsyncPanel loading={false} error="模型未就绪" empty={false}>内容</AsyncPanel>);
    expect(screen.getByRole('alert')).toHaveTextContent('模型未就绪');
  });

  it('模式标签始终说明合成演练', () => {
    render(<ModeBadge runPurpose="local" />);
    expect(screen.getByText('本地演练 · 合成数据')).toBeInTheDocument();
  });

  it('确认框支持取消与确认', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ConfirmDialog title="作废本轮" description="将冲销积分" onConfirm={onConfirm} onCancel={onCancel} />);
    await user.click(screen.getByRole('button', { name: '取消' }));
    await user.click(screen.getByRole('button', { name: '确认作废本轮' }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('确认框约束焦点、支持 Escape 并恢复触发点', async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const dialog = <ConfirmDialog title="作废本轮" description="将冲销积分" onConfirm={vi.fn()} onCancel={onCancel} />;
    const view = render(<button>打开确认</button>);
    const trigger = screen.getByRole('button', { name: '打开确认' });
    trigger.focus();
    view.rerender(<><button>打开确认</button>{dialog}</>);
    expect(screen.getByRole('button', { name: '取消' })).toHaveFocus();
    screen.getByRole('button', { name: '确认作废本轮' }).focus();
    await user.tab();
    expect(screen.getByRole('button', { name: '关闭' })).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole('button', { name: '确认作废本轮' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(onCancel).toHaveBeenCalledOnce();
    view.rerender(<button>打开确认</button>);
    expect(trigger).toHaveFocus();
  });

  it('外部文本只按文本渲染', () => {
    render(<TextCell>{'<img src=x onerror=alert(1)>'}</TextCell>);
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });
});
