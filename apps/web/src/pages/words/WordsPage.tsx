import { useEffect, useMemo, useState } from 'react';
import { FileUp, Plus, Search } from 'lucide-react';
import { fixtureAdapter } from '../../api/fixture/adapter';
import type { DemoResult, PuzzleView, WordbankView } from '../../api/fixture/types';
import { ConfirmDialog, FieldError, InlineNotice, PageHeader, Panel, StatusPill } from '../../components/ui';

const PAGE_SIZE = 20;

type Props = {
  initialData: WordbankView;
  onCommand?: (command: string) => Promise<DemoResult>;
  stale?: boolean;
};

export function WordsPage({ initialData, onCommand = fixtureAdapter.executeDemoCommand, stale = false }: Props) {
  const [selectedId, setSelectedId] = useState(initialData.puzzles[0]?.id ?? '');
  const [drafts, setDrafts] = useState<Record<string, PuzzleView>>({});
  const [dirtyIds, setDirtyIds] = useState<Set<string>>(() => new Set());
  const [pendingSelection, setPendingSelection] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [deferredQuery, setDeferredQuery] = useState('');
  const [category, setCategory] = useState('全部分类');
  const [difficulty, setDifficulty] = useState('全部难度');
  const [state, setState] = useState('全部状态');
  const [page, setPage] = useState(1);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const selected = drafts[selectedId] ?? initialData.puzzles.find((puzzle) => puzzle.id === selectedId);
  const dirty = dirtyIds.has(selectedId);
  const categories = useMemo(() => [...new Set(initialData.puzzles.map((puzzle) => puzzle.category))], [initialData.puzzles]);
  const filtered = useMemo(() => initialData.puzzles.filter((puzzle) => {
    const matchesQuery = `${puzzle.id}${puzzle.word}${puzzle.category}`.toLowerCase().includes(deferredQuery.trim().toLowerCase());
    return matchesQuery && (state === '全部状态' || puzzle.state === state) && (category === '全部分类' || puzzle.category === category) && (difficulty === '全部难度' || puzzle.difficulty === Number(difficulty));
  }), [initialData.puzzles, deferredQuery, state, category, difficulty]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visiblePuzzles = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  useEffect(() => {
    const timer = window.setTimeout(() => { setDeferredQuery(query); setPage(1); }, 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    if (!dirty) return;
    const protectDraft = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', protectDraft);
    return () => window.removeEventListener('beforeunload', protectDraft);
  }, [dirty]);

  function updateSelected(patch: Partial<PuzzleView>) {
    if (!selected || selected.state !== '草稿' || stale || pending) return;
    setDrafts((current) => ({ ...current, [selected.id]: { ...selected, ...patch } }));
    setDirtyIds((current) => new Set(current).add(selected.id));
    setError('');
  }

  function selectPuzzle(id: string) {
    if (id === selectedId) return;
    if (dirty) setPendingSelection(id);
    else setSelectedId(id);
  }

  function discardDraft() {
    if (!pendingSelection) return;
    setDrafts((current) => { const next = { ...current }; delete next[selectedId]; return next; });
    setDirtyIds((current) => { const next = new Set(current); next.delete(selectedId); return next; });
    setSelectedId(pendingSelection);
    setPendingSelection(null);
    setError('');
  }

  async function saveDraft() {
    if (!selected || !dirty || stale || pending) return;
    setPending(true);
    setMessage('');
    setError('');
    try {
      const result = await onCommand('puzzle.save_draft');
      if (result.status === 'applied') {
        setDirtyIds((current) => { const next = new Set(current); next.delete(selected.id); return next; });
        setMessage(`${result.message} · 草稿版本 v${selected.version}`);
      } else {
        setError(`${result.message} · 草稿已保留`);
      }
    } catch (reason) {
      setError(`${reason instanceof Error && reason.message === 'VERSION_CONFLICT' ? '版本冲突，请重新读取后比较' : '保存失败'} · 草稿已保留`);
    } finally {
      setPending(false);
    }
  }

  return <div className="page words-page">
    <PageHeader eyebrow="04 / AUTHORIZED WORDBANK" title="题库管理" description="草稿、质检、审核、发布是不同工作流状态；已发布版本只读，当前轮题目始终冻结。" actions={<button className="button" disabled={stale || pending} onClick={() => setMessage('新建草稿：待真实 API 接入')}><Plus size={16} />新建草稿</button>} />
    {stale && <InlineNotice tone="warn">题库数据已过期；可查看旧数据，依赖旧版本的写操作已禁用。</InlineNotice>}
    <div className="grid wide-sidebar">
      <div className="stack">
        <Panel title="题目列表" eyebrow={`${filtered.length} / ${initialData.puzzles.length} SYNTHETIC`}>
          <div className="form-grid word-filters">
            <label className="field span-two"><span>搜索题目</span><div className="input-with-icon"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="ID、答案或分类" /></div></label>
            <label className="field"><span>分类</span><select value={category} onChange={(event) => { setCategory(event.target.value); setPage(1); }}><option>全部分类</option>{categories.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label className="field"><span>难度</span><select value={difficulty} onChange={(event) => { setDifficulty(event.target.value); setPage(1); }}><option>全部难度</option>{[1, 2, 3, 4, 5].map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
            <label className="field"><span>发布状态</span><select value={state} onChange={(event) => { setState(event.target.value); setPage(1); }}><option>全部状态</option><option>草稿</option><option>待质检</option><option>待审核</option><option>已发布</option><option>已禁用</option></select></label>
          </div>
          {filtered.length === 0 ? <div className="async-panel"><span>{initialData.puzzles.length === 0 ? '暂无题目，请先新建草稿' : '筛选无结果'}</span>{initialData.puzzles.length > 0 && <button className="button ghost" onClick={() => { setQuery(''); setDeferredQuery(''); setCategory('全部分类'); setDifficulty('全部难度'); setState('全部状态'); setPage(1); }}>清除筛选</button>}</div> : <div className="word-list mt">{visiblePuzzles.map((puzzle) => <button key={puzzle.id} className={`word-list-item ${selectedId === puzzle.id ? 'selected' : ''}`} onClick={() => selectPuzzle(puzzle.id)}><span><strong>{puzzle.id} · {puzzle.word}</strong><small>{puzzle.category} · 难度 {puzzle.difficulty} · v{puzzle.version}</small></span><StatusPill tone={puzzle.state === '已发布' ? 'good' : puzzle.state === '草稿' ? 'info' : 'warn'}>{puzzle.state}</StatusPill></button>)}</div>}
          {filtered.length > 0 && <div className="button-row mt"><button className="button ghost" disabled={page === 1} onClick={() => setPage((current) => Math.max(1, current - 1))}>上一页</button><span className="caption">第 {page} / {pageCount} 页 · 每页 {PAGE_SIZE} 条</span><button className="button ghost" disabled={page === pageCount} onClick={() => setPage((current) => Math.min(pageCount, current + 1))}>下一页</button></div>}
        </Panel>
        <Panel title="来源与权属" eyebrow="PROVENANCE"><p className="caption">仅使用获授权题库。每题保留来源、使用依据、版本和校准信息；合成坏行不会进入发布集合。</p></Panel>
      </div>

      <div className="stack">
        <Panel title="题目详情" eyebrow={selected ? `${selected.id} · VERSION ${selected.version}` : 'NO SELECTION'}>
          {selected && <>
            <div className="form-grid">
              <label className="field"><span>答案</span><input value={selected.word} readOnly={selected.state !== '草稿'} onChange={(event) => updateSelected({ word: event.target.value })} /></label>
              <label className="field"><span>题目分类</span><input value={selected.category} readOnly={selected.state !== '草稿'} onChange={(event) => updateSelected({ category: event.target.value })} /></label>
              <label className="field"><span>题目难度</span><input type="number" min={1} max={5} value={selected.difficulty} readOnly={selected.state !== '草稿'} onChange={(event) => updateSelected({ difficulty: Number(event.target.value) })} /></label>
              <label className="field"><span>校准</span><input value={selected.calibration} readOnly /></label>
              <label className="field span-two"><span>审核别名</span><input value={selected.aliases.join('、')} readOnly={selected.state !== '草稿'} onChange={(event) => updateSelected({ aliases: event.target.value.split(/[、，,\n]/).map((item) => item.trim()).filter(Boolean) })} /></label>
              <label className="field span-two"><span>预审提示</span><textarea value={selected.hints.join('\n')} readOnly={selected.state !== '草稿'} onChange={(event) => updateSelected({ hints: event.target.value.split('\n') })} /></label>
              <label className="field span-two"><span>解释</span><textarea value={selected.explanation} readOnly={selected.state !== '草稿'} onChange={(event) => updateSelected({ explanation: event.target.value })} /></label>
              <label className="field span-two"><span>来源／使用依据</span><input value={selected.source} readOnly={selected.state !== '草稿'} onChange={(event) => updateSelected({ source: event.target.value })} /></label>
            </div>
            {selected.state === '草稿' ? <><p className="caption mt">编辑版本 v{selected.version} · 保存附带 expected_version{dirty ? ' · 有未保存修改' : ''}</p><button className="button mt" disabled={!dirty || stale || pending} onClick={saveDraft}>{pending ? '保存中…' : '保存草稿'}</button></> : <InlineNotice tone="warn">已发布版本只读；修订必须创建新草稿</InlineNotice>}
            <div className="button-row mt"><button className="button secondary" disabled={stale || pending || selected.state === '已发布'} onClick={() => setMessage('质检任务已创建 · 合成演练，不代表质量通过')}>提交质检</button><button className="button secondary" disabled={stale || pending || selected.state !== '待审核'} onClick={() => setMessage('审核与发布是独立命令；当前仅记录合成操作')}>审核候选</button></div>
          </>}
          {message && <InlineNotice>{message}</InlineNotice>}
          {error && <FieldError>{error}</FieldError>}
        </Panel>
      </div>
    </div>

    <Panel title="CSV 导入校验" eyebrow="SERVER REPORT IS AUTHORITATIVE" className="mt" action={<button className="button secondary" disabled={stale || pending} onClick={() => setMessage('CSV 上传：待真实 API 接入')}><FileUp size={16} />选择 CSV</button>}>
      <div className="grid four">
        <div><span className="field-label">文件</span><p>{initialData.importReport.file}</p></div>
        <div><span className="field-label">总行</span><p className="big-number">{initialData.importReport.total}</p></div>
        <div><span className="field-label">有效</span><p className="big-number teal">{initialData.importReport.valid}</p></div>
        <div><span className="field-label">重复</span><p className="big-number amber">{initialData.importReport.duplicate}</p></div>
      </div>
      <div className="table-wrap"><table><thead><tr><th>位置</th><th>错误</th><th>处理</th></tr></thead><tbody>{initialData.importReport.errors.map((reportError) => <tr key={reportError.row}><td><strong>第 {reportError.row} 行 · {reportError.column}</strong></td><td>{reportError.message}</td><td>排除该行</td></tr>)}</tbody></table></div>
      <p className="caption">确认前不会写入或发布；服务端质检报告才是权威，前端解析不能替代。</p>
      <button className="button" disabled={stale || pending} onClick={() => setMessage(`已提交 ${initialData.importReport.valid} 条有效行确认 · 合成演练，未发布`)}>确认 {initialData.importReport.valid} 条有效行（合成）</button>
    </Panel>
    {pendingSelection && <ConfirmDialog title="未保存草稿" description={`${selected?.id ?? '当前题目'}有未保存修改。继续编辑，或放弃草稿并切换到 ${pendingSelection}。`} cancelLabel="继续编辑" confirmLabel="放弃草稿" onCancel={() => setPendingSelection(null)} onConfirm={discardDraft} />}
  </div>;
}
