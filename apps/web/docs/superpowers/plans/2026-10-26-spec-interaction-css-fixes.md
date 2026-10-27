# Spec Interaction and CSS Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 当前会话禁止子代理、worktree 切换与 Git 提交；在用户授权的 `apps/web/**` 边界内直接执行。

**Goal:** 修复审查确认的 16 项 Spec 页面、交互与 OBS CSS 缺口，同时保持合成数据与真实能力边界。

**Architecture:** 保留 fixtureAdapter 与现有路由。通过小型受控组件状态实现合成交互，命令只有 `status === 'applied'` 才更新合成状态；partial-error 通过 FixtureQuery 上下文显式禁用依赖旧数据的动作。OBS 继续使用固定设计画布，但将关键内容限制在安全区，并以静态换行和异常长文本字号策略消除滚动裁切。

**Tech Stack:** React 19、TypeScript、Zustand、TanStack Query、Vitest、Testing Library、Playwright、原生 CSS。

---

## 文件结构

- `src/styles/obs.css`：安全区、规定字号、静态提示与榜单行高。
- `src/pages/obs/ObsPage.tsx`、`src/features/round/Clock.tsx`：真实 mode/runPurpose 文案、契约错误、轮间倒计时。
- `src/pages/studio/StudioPage.tsx`、`src/api/fixture/types.ts`、`src/api/fixture/data.ts`：命令结果状态与预审提示确认。
- `src/pages/setup/SetupPage.tsx`：真实 preview 路由入口。
- `src/pages/words/WordsPage.tsx`：筛选、去抖、分页、完整草稿字段与未保存保护。
- `src/pages/scoring/ScoringPage.tsx`：异常与 pending 清理。
- `src/pages/replay/ReplayPage.tsx`：筛选与回放控制状态。
- `src/app/FixtureQuery.tsx`、`src/app/App.tsx`、`src/pages/analytics/AnalyticsPage.tsx`：过期状态传播和导出禁用。
- `src/pages/settings/SettingsPage.tsx`：撤票与维护确认、活动场守卫。
- 邻近测试与 `tests/*.spec.ts`：每项行为的失败—修复—通过证据。

### Task 1: OBS 状态与轮间时钟

**Files:** Modify `src/pages/obs/ObsPage.tsx`, `src/features/round/Clock.tsx`; Test `src/pages/obs/ObsPage.test.tsx`, `src/features/round/Clock.test.tsx`.

- [ ] 写失败测试：production 投影显示正式模式；CONTRACT_VERSION_MISMATCH 显示“展示版本不匹配”；INTERMISSION 从 nextStartAt 倒计时并在到零后显示等待确认。
- [ ] 运行 `pnpm exec vitest run src/pages/obs/ObsPage.test.tsx src/features/round/Clock.test.tsx`，确认新增断言失败。
- [ ] ObsHeader 接收 `mode` 与 `runPurpose`；契约错误与序列错误分渲染。Clock 复用 `serverTime + performance.now()` 基准：

```ts
const nextAt = round.nextStartAt ? Date.parse(round.nextStartAt) : NaN;
const remaining = Number.isFinite(nextAt) ? Math.max(0, nextAt - estimatedServerNow) : 0;
// remaining > 0 显示 mm:ss；否则显示“等待确认”，不自行换轮。
```

- [ ] 重跑同一命令，预期全部通过。

### Task 2: OBS 安全区与静态长文本 CSS

**Files:** Modify `src/styles/obs.css`, `src/pages/obs/ObsPage.tsx`; Test `tests/obs.spec.ts`.

- [ ] 扩展 Playwright：默认横屏 computed font-size 提示 34px、词榜 32px；长提示 `scrollHeight <= clientHeight`；`.obs-rule/.obs-feedback/.obs-ranking/.obs-players` 的 bounding rect 位于 preview 安全区内；八行文本不被裁切。
- [ ] 运行 `pnpm build && PLAYWRIGHT_BROWSERS_PATH=0 pnpm exec playwright test tests/obs.spec.ts`，确认旧 CSS 失败。
- [ ] 横屏使用紧凑顶部、规则同行动作说明、安全区底部留白；竖屏使用 120/300px 上下安全留白。正常提示横屏 34px、词榜 32px；异常长提示通过 `data-long-hints` 使用较小静态换行字号，禁止依赖只读滚动区。榜单行 `line-height` 与 grid 行高匹配。
- [ ] 重跑双尺寸 normal/long-content/revealed、0.9/1/1.2 字号与安全区几何测试，预期全部通过。

### Task 3: 开播、主播台与设置危险操作

**Files:** Modify `src/pages/setup/SetupPage.tsx`, `src/pages/studio/StudioPage.tsx`, `src/pages/settings/SettingsPage.tsx`, `src/api/fixture/types.ts`, `src/api/fixture/data.ts`; Test 邻近页面测试与 `tests/studio.spec.ts`.

- [ ] 写失败测试：P01 预览链接指向 `/obs.html`；非 applied 命令不修改回合；发布提示确认框含确切文本；撤票和维护需确认，活动场禁维护。
- [ ] 运行相关 Vitest，确认失败。
- [ ] P01 使用 `<a href="/obs.html" target="_blank" rel="noreferrer">`。Studio 只在 `result.status === 'applied'` 后更新合成状态；增加 `nextHint` 合成字段和确认描述。Settings 增加撤票/备份 ConfirmDialog 与 `activeSession` 守卫，仍只返回“待真实 API 接入”。
- [ ] 重跑页面测试，预期通过。

### Task 4: 题库、评分与回放交互

**Files:** Modify `src/pages/words/WordsPage.tsx`, `src/pages/scoring/ScoringPage.tsx`, `src/pages/replay/ReplayPage.tsx`, `src/api/fixture/types.ts`, `src/api/fixture/data.ts`; Test 邻近页面测试与 `tests/wordbank.spec.ts`, `tests/diagnostics.spec.ts`.

- [ ] 写失败测试：分类/难度筛选、300ms 去抖、筛选重置页码、20 条分页；草稿字段编辑与放弃确认；评测拒绝后恢复；reason 过滤；回放 start/pause/resume/cancel 按状态启用。
- [ ] 运行相关 Vitest，确认失败。
- [ ] Words 使用 `deferredQuery`、`page` 和 `PAGE_SIZE = 20`；PuzzleView 增加 `aliases/hints/explanation`，草稿用统一 `updateSelected(patch)`；dirty 时内部切换和离页均需放弃确认。保存保留版本与失败草稿。
- [ ] Scoring 使用 `try/catch/finally`，错误后 `setPending(false)` 并显示未生成真实分数。
- [ ] Replay 使用受控 session/reason/cursor 和过滤结果；按 job 状态启用 start/pause/resume/cancel，取消二次确认，pending 禁重复。
- [ ] 重跑相关测试，预期通过。

### Task 5: 过期数据依赖动作

**Files:** Modify `src/app/FixtureQuery.tsx`, `src/app/App.tsx`, `src/pages/analytics/AnalyticsPage.tsx`, `src/pages/words/WordsPage.tsx`; Test `src/app/FixtureQuery.test.tsx`, `src/pages/analytics/AnalyticsPage.test.tsx`, `tests/relay-state.spec.ts`.

- [ ] 写失败测试：partial-error 向页面提供 `stale=true`，复盘导出与依赖旧版本的题库写入禁用。
- [ ] 将 children 扩展为 `(data, { stale })`，Analytics/Words 接收并禁用对应动作；正常场景 stale=false。
- [ ] 重跑共享容器和页面测试，预期通过。

### Task 6: 完整验证与文档

**Files:** Modify `apps/web/README.md`, `tests/*.spec.ts`; 不修改后端。

- [ ] 更新合成交互与 CSS 验证说明，不宣称真实 API、Windows、OBS/CEF 或平台验收。
- [ ] 运行 `pnpm test -- --run && pnpm build && PLAYWRIGHT_BROWSERS_PATH=0 pnpm test:e2e`，预期 0 失败。
- [ ] 扫描 `dist/obs.html` 的 script/modulepreload 敏感字段并确认无 `.map`；检查后台无本次改动、无残留 Vite/Playwright 进程。

## 自检

- 16 项 finding 分别由 Tasks 1–5 覆盖；Task 6 验证。
- 不加入真实网络、鉴权、票据或后端实现，不改变 Spec 权威边界。
- `applied`、`.obs-hints`、`nextStartAt`、`stale`、`PAGE_SIZE` 等名称在所有任务保持一致。
- 无 TBD/TODO；不提交、不推送、不切换 worktree。
