# WordHeat 前端页面实施计划

> **执行方式：** 在当前会话使用 executing-plans 逐任务实施；不使用子代理，不修改 `apps/web/` 之外的代码，不执行 Git 提交。

**Goal:** 按前端 Spec 第 0 节交付八类可运行页面、明确的合成数据交互、Studio/OBS 双入口及组件测试。

**Architecture:** React + TypeScript + Vite 单工程双入口。页面只依赖 fixture 端口和视图模型；公开 OBS 入口不导入 Studio 路由或私有 fixture。TanStack Query 管理异步查询，Zustand 保存本地演练投影与无秘密偏好。

**Tech Stack:** React 19、TypeScript、Vite、TanStack Query、Zustand、React Router、Lucide React、Vitest、Testing Library、Playwright。

---

## 文件结构

- 工程：`package.json`、`pnpm-lock.yaml`、`vite.config.ts`、`tsconfig*.json`、`index.html`、`obs.html`。
- 装配：`src/app/{main,displayMain,App,AuthBoundary,ErrorBoundary}.tsx`。
- 页面端口：`src/api/fixture/{types,data,adapter}.ts(x)`，不创建伪造的 generated 类型。
- 公共 UI：`src/components/ui/` 与 `src/components/charts/ParticipationChart.tsx`。
- 页面：`src/pages/{setup,studio,obs,words,scoring,replay,analytics,settings}/`。
- 功能：`src/features/{display,round,guesses,connection,wordbank,scoring,diagnostics}/`。
- 状态：`src/stores/{displayStore,studioStore,preferencesStore}.ts`。
- 安全与样式：`src/security/`、`src/styles/{tokens,studio,obs}.css`。
- 测试：`src/**/*.test.ts(x)`、`apps/web/tests/*.spec.ts`。

### Task 1：工程与测试基线

- [ ] 创建 Vite/React/TypeScript 双入口工程，`dev` 脚本通过 `vite --host 0.0.0.0` 转发宿主和端口参数。
- [ ] 先写 `src/app/routes.test.ts`，断言八个私有路径和 OBS 路径定义完整；运行测试确认因实现缺失失败。
- [ ] 创建 `src/app/routes.ts` 导出路由常量，再运行 `pnpm test -- --run` 验证通过。
- [ ] 执行 `pnpm install`、`pnpm build`，预期 TypeScript 与两个 HTML 入口构建成功。

### Task 2：合成视图模型与 fixture 端口

- [ ] 在 `src/api/fixture/types.ts` 定义 `RoundView`、`FeedbackView`、`ReadinessView`、`TraceView`、`AnalyticsView` 等页面视图模型，不定义线上 DTO。
- [ ] 在 `src/api/fixture/data.ts` 写入全部合成数据，统一 `synthetic: true`，包含运行、暂停、重连、揭晓、空数据和错误场景。
- [ ] 先写 `src/api/fixture/adapter.test.ts`，断言 fixture 查询返回合成标记、演练命令只更新本地状态、公开快照不包含 `answer/aliases/target_vector/platform_uid`。
- [ ] 实现 `fixtureAdapter` 的 `getSetup`、`getStudio`、`getPublicDisplay`、`getWords`、`getScoring`、`getDiagnostics`、`getAnalytics`、`getSettings` 与 `executeDemoCommand`。
- [ ] 运行适配器测试并确认通过；真实能力统一返回 `待真实 API 接入`，不得伪造成功。

### Task 3：公共主题与基础组件

- [ ] 建立深色墨蓝、青绿、琥珀色 tokens，统一 14–16px 后台字号、清晰 focus、44px 主要点击目标与 reduced-motion。
- [ ] 先写 `src/components/ui/ui.test.tsx`，覆盖 `AsyncPanel` 加载/空/错误、`ModeBadge` 演练标识、`ConfirmDialog` 取消和确认、外部文本作为文本节点。
- [ ] 实现 `AsyncPanel`、`ConfirmDialog`、`FieldError`、`TextCell`、`ModeBadge`、`PageHeader`、`StatusPill`、`MetricCard`。
- [ ] 实现 `AppShell` 导航、全局“合成数据演练”条和无秘密偏好控制。
- [ ] 运行组件测试和生产构建，确认无 `dangerouslySetInnerHTML`。

### Task 4：OBS 公开页面与同步算法

- [ ] 先写 `src/features/display/syncMachine.test.ts`，覆盖 BigInt 大序号、重复、乱序、gap、完整绝对值替换和 `resume_ready` 前禁止回执。
- [ ] 实现纯函数 `compareSeq`、`reduceDisplayEvent`、`createSyncState`，非法版本和 gap 显式失败。
- [ ] 先写 `src/features/display/receiptQueue.test.ts`，覆盖 preview 不回执、代次隔离、同批重试、processed 才释放额度。
- [ ] 实现内存 `receiptQueue`，本轮只返回演练回执状态，不发送真实网络请求。
- [ ] 创建独立 `obs.html`/`displayMain.tsx`，页面包含规则、短码、分类、字格、倒计时、提示、最新反馈、八词榜、场次前三、揭晓及传输状态。
- [ ] 在 `tests/obs.spec.ts` 覆盖 1080×1920、1920×1080、减少动画、长昵称和无管理按钮。

### Task 5：准备、主播台和设置

- [ ] 先写 `tests/studio.spec.ts` 的组件级场景：创建草稿与开始分离、pending 禁重复、旧版本冲突、当前轮配置不变、ready 布尔权威。
- [ ] 实现 `/setup`：场次表单、连接与依赖卡、三类 readiness、试词、布局预览及开始演练；正式/测试按钮显示未接入而非假成功。
- [ ] 实现 `/studio/:sessionId`：谜底遮罩、回合信息、私有榜单、开始/暂停/恢复/跳题/作废/提示/结束及恢复确认。
- [ ] 实现 `/settings`：预览偏好、下一轮规则差异、展示票据演练、模型/备份状态及维护请求说明；任意路径/命令输入不得出现。
- [ ] 所有演练命令显示“本地演练已应用”，真实能力显示“待真实 API 接入”。

### Task 6：题库与评分实验室

- [ ] 先写 `tests/wordbank.spec.ts` 场景：草稿编辑、发布版本只读、CSV 行列错误、有效行确认、盲标锁和版本展示。
- [ ] 实现 `/words`：搜索筛选、题表、草稿编辑、合成 CSV 报告、审核发布状态和禁用说明。
- [ ] 实现 `/scoring`：评测表单、合成任务状态、raw/display 分列、人工等级、模型/校准/题库版本和盲标锁。
- [ ] 不提供 `pass=true`、随机评分、真实发布或真实模型调用；发布按钮只展示演练状态。

### Task 7：诊断回放与场次复盘

- [ ] 先写 `tests/diagnostics.spec.ts` 场景：消息过滤、五确认分栏、无证据显示未知、回放隔离和来源统计隔离。
- [ ] 实现 `/replay`：正式只读追踪、脱敏时间线、replay 任务及 start/pause/resume/cancel 演练控制。
- [ ] 实现 `/analytics/:sessionId`：指标卡、参与趋势、轮次表、统计时点和 CSV 导出确认。
- [ ] 在 `src/security/csv.ts` 实现公式首字符保护、双引号转义和字段白名单，并用单元测试验证。
- [ ] 无观看分母时显示不可计算，不生成漏词率或停留率。

### Task 8：连接状态、集成验证与交付

- [ ] 在 `src/features/connection/status.ts` 实现 round/transport/readiness 组合文案，覆盖短重连不暂停、gap/租约失效禁开播、test_live 不等于 production。
- [ ] 创建 `tests/relay-state.spec.ts`，用实际纯函数与故障 fixture 验证状态，不宣称 WP30 真实双端完成。
- [ ] 运行 `pnpm test -- --run`、`pnpm build`、`pnpm test:e2e`，保存实际结果；若系统浏览器不可用，明确记录未执行项。
- [ ] 检查构建产物不包含私有谜底、aliases、向量、平台 secret 和私有 source map。
- [ ] 更新 `apps/web/README.md` 记录启动、测试、构建、页面完成范围及未验证边界。
- [ ] 最终只启动临时开发服务器做浏览器验证，结束前停止，不遗留 Node/Vite 进程。

## 完成审计

只有八页可访问、测试和构建通过、公开包隔离检查通过、README 边界完整，才声明“页面层完成”。不得更新 WP 状态，不声明真实 API、模型、relay、Windows、OBS CEF、固定 RC 或 G0/G1/G2 验收通过。
