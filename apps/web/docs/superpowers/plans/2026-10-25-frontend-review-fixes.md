# 前端审查问题修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 当前会话禁止子代理，且用户要求直接执行、不提交 Git。

**Goal:** 仅修改 `apps/web/**`，修复审查确认的 10 项问题并达到前端 Spec 页面层完成口径。

**Architecture:** 保留 fixture 适配器，以可控合成场景驱动共享页面状态。OBS 通过合成 display bootstrap/store 连接同步、真实 DOM 回执、权威时钟和公开渲染；偏好由独立版本化存储提供。

**Tech Stack:** React 19、TypeScript、Vite、TanStack Query、Zustand、Vitest、Testing Library、Playwright。

---

## 文件结构

- `src/api/fixture/types.ts`：公开/私有合成视图类型，允许空轮并携带权威时钟、授权和稳定玩家项 ID。
- `src/api/fixture/data.ts`、`src/test/fixtures/display.ts`：正常及边界合成数据。
- `src/api/fixture/scenarios.ts`：URL 场景读取和公开场景构造，不含秘密。
- `src/app/FixtureQuery.tsx`：共享首次加载、空、局部错误、403、契约错误容器。
- `src/app/App.tsx`：路由接入共享容器。
- `src/features/display/syncMachine.ts`：连续投影与 ready 授权。
- `src/features/display/receiptQueue.ts`：授权、received、processed 与代次边界。
- `src/features/display/DisplayBootstrap.tsx`：合成快照/事件/ready、可见性与实际 DOM 回执。
- `src/features/display/useObsScale.ts`：双设计画布等比缩放。
- `src/features/round/Clock.tsx`：权威时钟计算与渲染。
- `src/pages/obs/ObsPage.tsx`、`src/styles/obs.css`：状态矩阵、空态、双画布和安全区。
- `src/stores/preferencesStore.ts`：版本化偏好、校验、持久化。
- `src/pages/settings/SettingsPage.tsx`：偏好和复制反馈。
- `src/pages/studio/StudioPage.tsx`：生命周期操作矩阵。
- `src/components/ui/index.tsx`：模态框焦点约束。
- `src/features/ranking/PlayerRanking.tsx`：稳定公开 key。
- 邻近 `*.test.ts(x)` 与 `tests/*.spec.ts`：先失败后通过的回归证据。

### Task 1: 公开视图、空轮与稳定玩家身份

**Files:** Modify `src/api/fixture/types.ts`, `src/api/fixture/data.ts`, `src/test/fixtures/display.ts`, `src/features/ranking/PlayerRanking.tsx`; Test `src/pages/obs/ObsPage.test.tsx`, `src/features/ranking/PlayerRanking.test.tsx`.

- [ ] 写失败测试：构造 `round:null`、`latest:null`、空 hints/ranking，断言显示等待开始、暂无提示和邀请参与，不显示 A7；两名同名玩家以不同 `id` 渲染两行。
- [ ] 运行 `pnpm test -- --run src/pages/obs/ObsPage.test.tsx src/features/ranking/PlayerRanking.test.tsx`，预期因无条件 `round` 和缺少稳定 ID 失败。
- [ ] 将模型调整为下列公开形状并更新 fixture：

```ts
export interface PublicClock {
  timeOrigin: 'relay' | 'local_rehearsal' | 'local_replay';
  deadlineAt: string | null;
  remainingMs: number;
}
export interface RoundView {
  id: string; code: string; category: string; answerLength: number;
  status: RoundStatus; remainingSeconds: number; version: number;
  clock: PublicClock; reason?: string; nextStartAt?: string | null;
}
export interface DisplayView extends Synthetic {
  sessionId: string; mode: 'rehearsal' | 'official' | 'replay';
  runPurpose: RunPurpose; serverTime: string; asOfSeq: string;
  authorization: 'AUTHORIZED' | 'UNAUTHORIZED';
  purpose: 'primary' | 'preview'; renderGeneration: string;
  round: RoundView | null; transport: TransportStatus;
  hints: string[]; latest: FeedbackView | null; feedback: FeedbackView[];
  wordRanking: Array<{ word: string; name: string; count: number; score: number }>;
  playerRanking: Array<{ id: string; name: string; points: number }>;
  reveal: null | { word: string; explanation: string; winner: string | null; reason: string };
  authorizedDeliveryIds: string[];
}
```

- [ ] `PlayerRanking` 使用 `key={player.id}`；OBS 对空轮、空 latest、空 hints、空榜单渲染明确静态文本。
- [ ] 重跑 Task 1 测试，预期 PASS。

### Task 2: 权威时钟与 OBS 状态矩阵

**Files:** Modify `src/features/round/Clock.tsx`, `src/pages/obs/ObsPage.tsx`; Test `src/features/round/Clock.test.tsx`, `src/pages/obs/ObsPage.test.tsx`.

- [ ] 写失败测试：fake timer 下 RUNNING 从 60 秒推进到 59 秒；PAUSED 不推进；DRAINING 显示“正在收尾”；UNAUTHORIZED 清空短码/榜单；FAULTED 显示故障；INTERMISSION 到零显示等待下一轮确认。
- [ ] 运行 `pnpm test -- --run src/features/round/Clock.test.tsx src/pages/obs/ObsPage.test.tsx`，预期 FAIL。
- [ ] 实现纯计算与组件 tick：

```ts
export function remainingClockMs(round: RoundView, estimatedServerNow: number): number {
  if (!round.clock.deadlineAt || round.status !== 'RUNNING') return Math.max(0, round.clock.remainingMs);
  const deadline = Date.parse(round.clock.deadlineAt);
  if (!Number.isFinite(deadline)) return 0;
  return Math.max(0, deadline - estimatedServerNow);
}
// 组件采用 serverTime + (performance.now() - baseMonoMs)，每 250ms 更新；
// serverTime/round 改变时重建基准，非法时间显示“时间校准中”。
```

- [ ] OBS 按 `authorization → transport → round.status` 决定画面；DRAINING 不显示参与邀请，REVEALED 显示静态 reveal，FAULTED/UNAUTHORIZED 清旧内容。
- [ ] 重跑 Task 2 测试，预期 PASS。

### Task 3: 同步、真实 DOM 回执与双画布

**Files:** Create `src/features/display/DisplayBootstrap.tsx`, `src/features/display/useObsScale.ts`, `src/features/display/DisplayBootstrap.test.tsx`; Modify `src/features/display/receiptQueue.ts`, `src/features/display/syncMachine.ts`, `src/app/displayMain.tsx`, `src/app/App.tsx`, `src/styles/obs.css`, `src/features/round/Reveal.tsx`; Test `src/features/display/*.test.ts(x)`.

- [ ] 写失败测试：bootstrap 经过 snapshot→连续事件→ready 才更新；preview/隐藏文档不 processed；可见且目标 DOM 存在时 processed；缺口显示重同步；1920×1080 与 1080×1920 容器保持比例。
- [ ] 运行 `pnpm test -- --run src/features/display`，预期 FAIL。
- [ ] `DisplayBootstrap` 持有 `SyncState` 和 `ReceiptQueue`，从公开 fixture 建立合成 ready；事件经 `reduceDisplayEvent` 后原子替换。渲染回调必须检查：

```ts
if (purpose === 'preview' || document.visibilityState !== 'visible') return;
const node = document.querySelector(`[data-feedback-id="${feedbackId}"]`);
if (!node?.textContent?.trim()) return;
requestAnimationFrame(() => queue.markProcessed(deliveryId));
```

- [ ] `ReceiptQueue` 保留 generation 与 128 上限；只在当前代次授权项上标记 received/processed，processed 才释放额度。
- [ ] `useObsScale` 以 `Math.min(viewportWidth/designWidth, viewportHeight/designHeight)` 计算缩放；OBS stage 居中且不裁切，安全区只在 preview 输出。
- [ ] `displayMain` 和路由 OBS 均使用 `DisplayBootstrap`，不再直接固定渲染 fixture。
- [ ] 重跑 `pnpm test -- --run src/features/display src/app`，预期 PASS。

### Task 4: 共享异步场景容器

**Files:** Create `src/api/fixture/scenarios.ts`, `src/app/FixtureQuery.tsx`, `src/app/FixtureQuery.test.tsx`; Modify `src/app/App.tsx`, `src/pages/*/*.test.tsx` as needed.

- [ ] 写失败测试：`?scenario=loading|empty|partial-error|forbidden|contract-error` 分别显示加载、空态下一步、保留旧内容并标过期、权限替换、版本不匹配。
- [ ] 运行 `pnpm test -- --run src/app/FixtureQuery.test.tsx`，预期 FAIL。
- [ ] 实现唯一场景解析：

```ts
export type FixtureScenario = 'normal' | 'loading' | 'empty' | 'partial-error' | 'forbidden' | 'contract-error';
export function readFixtureScenario(search: string): FixtureScenario {
  const value = new URLSearchParams(search).get('scenario');
  return ['loading','empty','partial-error','forbidden','contract-error'].includes(value ?? '')
    ? value as FixtureScenario : 'normal';
}
```

- [ ] `FixtureQuery` 在 partial-error 中同时渲染旧 children 和 `role=alert` 的过期提示；forbidden/contract-error 不渲染依赖动作；empty 显示“当前筛选下暂无数据，请返回开播准备或调整筛选”。
- [ ] 所有私有路由复用该容器；保留 TanStack Query 首次错误。
- [ ] 重跑共享容器和 App 测试，预期 PASS。

### Task 5: 主播台生命周期操作矩阵

**Files:** Modify `src/pages/studio/StudioPage.tsx`, `src/pages/studio/StudioPage.test.tsx`.

- [ ] 写失败测试：IDLE 可开始新轮不可 resume；DRAINING 禁暂停/恢复/新轮并显示收尾；REVEALED/INTERMISSION 显示原因与下一轮；FAULTED 显示恢复确认；跳题、作废、结束均需确认且 pending 禁重复。
- [ ] 运行 `pnpm test -- --run src/pages/studio/StudioPage.test.tsx`，预期 FAIL。
- [ ] 以 `round?.status ?? 'IDLE'` 计算允许命令；增加 `round.create`、`round.void`、`session.recover`，统一 confirm descriptor 和 pending 错误处理。跳题后只显示等待权威 VOID，不直接清榜。
- [ ] 空轮时页眉、指标和说明不读取 `round.id/code/version`；谜底仍在私有 Studio 内默认遮罩。
- [ ] 重跑 Studio 测试，预期 PASS。

### Task 6: 版本化偏好、预览应用与复制反馈

**Files:** Create `src/stores/preferencesStore.ts`, `src/stores/preferencesStore.test.ts`; Modify `src/pages/settings/SettingsPage.tsx`, `src/pages/settings/SettingsPage.test.tsx`, `src/features/display/DisplayBootstrap.tsx`, `src/styles/obs.css`.

- [ ] 写失败测试：合法偏好可保存/读取；坏 JSON、错误布局、越界 fontScale 回默认；key 含 `v0.1.0`；布局被保存；复制按钮不显示“已复制”。
- [ ] 运行 `pnpm test -- --run src/stores/preferencesStore.test.ts src/pages/settings/SettingsPage.test.tsx`，预期 FAIL。
- [ ] 实现：

```ts
export const PREFERENCES_KEY = 'wordheat-display-preferences:v0.1.0';
export const defaultDisplayPreferences = {
  layout: 'portrait', fontScale: 1, safeArea: true, reducedMotion: false,
} as const;
export type DisplayPreferences = {
  layout: 'portrait' | 'landscape'; fontScale: 0.9 | 1 | 1.1 | 1.2;
  safeArea: boolean; reducedMotion: boolean;
};
// loadDisplayPreferences 用 try/catch、白名单与类型检查；save 只写上述无秘密字段。
```

- [ ] Settings 使用受控 layout/fontScale，变更立即保存；恢复默认同时持久化。DisplayBootstrap 仅 preview 读取并应用偏好。
- [ ] 复制按钮改为“复制链接待真实 API 接入”，不调用虚假成功消息。
- [ ] 重跑 Task 6 测试，预期 PASS。

### Task 7: 模态框焦点与长文本可访问性

**Files:** Modify `src/components/ui/index.tsx`, `src/components/ui/ui.test.tsx`, `src/styles/studio.css`, `src/styles/obs.css`.

- [ ] 写失败测试：打开确认框聚焦取消；Tab 从最后按钮回到第一个，Shift+Tab 反向循环；Escape 取消；关闭后焦点回触发按钮。
- [ ] 运行 `pnpm test -- --run src/components/ui/ui.test.tsx`，预期 FAIL。
- [ ] ConfirmDialog 保存 `document.activeElement`，查询对话框内可聚焦元素，拦截 Tab/Shift+Tab，并在 cleanup 恢复焦点；Escape 保持取消。
- [ ] 对外部长文本使用 `overflow-wrap:anywhere`，反馈维持 `aria-live=polite`，高频列表不增加 live region。
- [ ] 重跑公共 UI 测试，预期 PASS。

### Task 8: 浏览器场景、文档与最终门禁

**Files:** Modify `tests/obs.spec.ts`, `tests/relay-state.spec.ts`, `tests/studio.spec.ts`, `README.md`; Create no backend files.

- [ ] 增加 Playwright：双尺寸 OBS 不溢出、preview 安全区、减少动画、空轮/故障/撤权、共享 empty/partial-error、同名玩家和纯键盘确认框。
- [ ] 先运行 `PLAYWRIGHT_BROWSERS_PATH=0 pnpm test:e2e`，确认新增断言能发现实现问题；修复后预期全部 PASS。
- [ ] README 更新合成场景 URL、偏好存储、页面层验证范围和真实能力未验证项。
- [ ] 运行完整门禁：

```bash
pnpm test -- --run
pnpm build
PLAYWRIGHT_BROWSERS_PATH=0 pnpm test:e2e
```

预期：所有组件/浏览器测试通过，双入口构建成功。
- [ ] 扫描 `dist/obs.html` 引用 JS 中的私有谜底、私有 fixture、aliases、target_vector、neighbors、platform UID/secret、票据字段，预期无匹配且无 `.map`。
- [ ] `git status --short` 仅出现 `apps/web/**` 与既有前端未跟踪内容；无后端变更，无残留 Vite/Playwright 进程。

## 自检

- Spec 覆盖：10 项 confirmed findings 分别由 Tasks 1–7 修复，Task 8 验证；真实 API/Windows/平台保持非目标。
- 占位符：无 TBD/TODO，无“适当处理”类不确定步骤。
- 类型一致性：`DisplayView.round` 可空；`PublicClock` 使用 camelCase 页面模型；`DisplayPreferences` 的 layout/fontScale 与 OBS/Settings 共用；回执只使用公开 delivery ID。
- Git：遵循用户和仓库当前授权，不提交、不推送、不切换 worktree。
