import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import { AlertCircle, ArrowUpRight, Check, FlaskConical, X } from 'lucide-react';
import type { RunPurpose } from '../../api/fixture/types';

export function TextCell({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <span className={className}>{children}</span>;
}

export function ModeBadge({ runPurpose }: { runPurpose: RunPurpose }) {
  const label = runPurpose === 'local' ? '本地演练 · 合成数据' : runPurpose === 'test_live' ? '平台测试 · 非正式' : '正式模式';
  return <span className={`mode-badge mode-${runPurpose}`}><FlaskConical size={14} aria-hidden />{label}</span>;
}

export function AsyncPanel({ loading, error, empty, children, emptyText = '当前筛选下暂无数据' }: { loading: boolean; error: string | null; empty: boolean; children: ReactNode; emptyText?: string }) {
  if (loading) return <div className="async-panel" role="status" aria-busy="true"><span className="loading-line" />正在加载当前视图…</div>;
  if (error) return <div className="async-panel error" role="alert"><AlertCircle size={20} />{error}</div>;
  if (empty) return <div className="async-panel"><span className="empty-index">00</span>{emptyText}</div>;
  return children;
}

export function StatusPill({ tone = 'neutral', children }: { tone?: 'good' | 'warn' | 'bad' | 'neutral' | 'info'; children: ReactNode }) {
  return <span className={`status-pill ${tone}`}><span className="status-dot" />{children}</span>;
}

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow: string; title: string; description: string; actions?: ReactNode }) {
  return <header className="page-header"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{actions && <div className="page-actions">{actions}</div>}</header>;
}

export function Panel({ title, eyebrow, children, className = '', action }: { title: string; eyebrow?: string; children: ReactNode; className?: string; action?: ReactNode }) {
  return <section className={`panel ${className}`}><div className="panel-heading"><div>{eyebrow && <span className="panel-eyebrow">{eyebrow}</span>}<h2>{title}</h2></div>{action}</div>{children}</section>;
}

export function MetricCard({ label, value, note, accent = false }: { label: string; value: string; note: string; accent?: boolean }) {
  return <article className={`metric-card ${accent ? 'accent' : ''}`}><span>{label}</span><strong>{value}</strong><small>{note}</small></article>;
}

export function InlineNotice({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warn' | 'bad' }) {
  return <div className={`inline-notice ${tone}`}><AlertCircle size={17} aria-hidden /><div>{children}</div></div>;
}

export function ConfirmDialog({ title, description, onConfirm, onCancel, confirmLabel, cancelLabel = '取消', pending = false }: { title: string; description: string; onConfirm: () => void; onCancel: () => void; confirmLabel?: string; cancelLabel?: string; pending?: boolean }) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const onCancelRef = useRef(onCancel);
  useEffect(() => { onCancelRef.current = onCancel; }, [onCancel]);
  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCancelRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? []);
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && (document.activeElement === first || !dialogRef.current?.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      previousFocus?.focus();
    };
  }, []);
  return <div className="dialog-backdrop"><div ref={dialogRef} className="dialog" role="dialog" aria-modal="true" aria-labelledby="dialog-title" aria-busy={pending}><button className="icon-button dialog-close" disabled={pending} onClick={onCancel} aria-label="关闭"><X size={18} /></button><span className="eyebrow">需要确认</span><h2 id="dialog-title">{title}</h2><p>{description}</p><div className="dialog-actions"><button ref={cancelRef} className="button secondary" disabled={pending} onClick={onCancel}>{cancelLabel}</button><button className="button danger" disabled={pending} onClick={onConfirm}><Check size={16} />{pending ? '确认处理中…' : confirmLabel ?? `确认${title}`}</button></div></div></div>;
}

export function TextLink({ children, onClick }: { children: ReactNode; onClick: () => void }) {
  return <button className="text-link" onClick={onClick}>{children}<ArrowUpRight size={14} /></button>;
}

export function FieldError({ children }: { children: ReactNode }) {
  return <span className="field-error" role="alert">{children}</span>;
}
