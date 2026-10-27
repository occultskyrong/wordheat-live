# 前端审查问题修复设计

日期：2026-10-25  
范围：仅 `apps/web/**`，不修改后端、共享契约、部署或其他工程，不提交 Git。

## 1. 目标与完成口径

修复 `.local/cr/review.json` 中 10 项 confirmed findings，使前端达到前端 Spec §0.3 的“页面层完成”口径。真实 API、WebSocket、鉴权、票据、Windows、OBS/CEF、真实模型和平台验收继续后置，不以合成结果冒充这些能力。

完成必须同时满足：

1. 八页现有路由和主流程不回归，规定的空、加载、错误、冲突、pending 与故障场景可通过合成数据检查。
2. OBS 支持空轮与完整状态矩阵、权威时钟、双设计画布、安全区 preview、减少动画和公开包隔离。
3. 同步与回执算法接入合成页面流程，真实渲染 processed 与预览／隐藏文档／旧代次边界可验证。
4. 主播台生命周期、共享异步状态、偏好、模态框焦点、复制反馈和同名玩家标识修复。
5. 组件测试、浏览器测试、生产构建和公开产物扫描通过，交付说明保留未验证边界。

## 2. 方案与模块边界

采用“共享合成场景＋明确状态边界”，保留现有 fixture 适配器，不在本轮搭建真实 API 客户端。

### 2.1 合成场景与视图模型

- 集中定义正常、空轮、PREPARING、PAUSED、DRAINING、REVEALED、INTERMISSION、FAULTED、重连、撤权、空数据和局部错误场景。
- 公共视图允许 `round=null`，时钟使用 Spec 的 `time_origin/deadline_at/remaining_ms` 与快照 `server_time`。
- 玩家公开项增加后端批准语义的稳定 `id` 或 `rank`，不使用昵称或原始平台 UID 作为身份。
- fixture 继续清楚标记合成，不包含私有答案、题库、向量或平台秘密。

### 2.2 OBS 状态投影与同步

- 增加合成 display bootstrap/store，连接现有 `syncMachine`、`ReceiptQueue` 和 `ObsPage`；独立 `displayMain` 与 `/obs/:sessionId` 使用同一公开流程。
- 快照原子替换公开投影；事件只在连续 seq 时应用；重复冲突、缺口、契约错误与撤权进入明确状态。
- 只有 ready 边界一致后允许回执；snapshot/replay 描述不授权履约。
- `ObsPage` 按 round、transport、authorization 组合渲染，不无条件读取旧短码或邀请输入。

### 2.3 权威时钟

- 快照采用 `server_time` 时记录 `performance.now()` 基准；RUNNING 显示权威 deadline 与估算服务器时间的差值。
- PAUSED 使用 `remaining_ms` 冻结；DRAINING 显示收尾；INTERMISSION 只显示下一轮等待，不自行换轮。
- 时钟不发送 timeout、不决定赢家；支持重新快照校准、标签页节流恢复与不确定度提示。

### 2.4 真实渲染回执

- received 只表示公开反馈已解码、授权匹配并进入可处理队列。
- processed 需要反馈 commit、当前文档可见、目标文本存在且错误边界未遮挡，并经过绘制机会核验。
- preview、隐藏文档、未授权、旧 generation 和 replay 描述不得 processed；换轮保留同 generation 未完成映射。
- 本轮以合成传输记录断言行为，不发送真实 receipts HTTP。

### 2.5 私有页共享状态与主播台

- 共享异步容器支持首次加载、空数据、局部错误保留旧数据并标采样时点、403/契约错误替换和依赖动作禁用。
- 合成适配器提供可控场景，页面不直接读取 fixture JSON。
- 主播台补齐开始新轮、暂停、恢复、跳题、作废、提示、结束和恢复确认的状态矩阵。
- DRAINING 禁暂停/恢复/新轮；REVEALED/INTERMISSION 显示原因与下一轮计划；FAULTED 显示修复原因和获准入口。
- 所有 pending 防重复，失败保留草稿；本地演练状态保持合成标识。

### 2.6 偏好、复制和公共交互

- 新增带应用版本 key 的 preferencesStore，读取校验、坏值回默认、立即保存，包含布局、字号、安全区和减少动画。
- 偏好只应用到 preview；主 OBS 不显示调试安全区。
- 无真实票据时复制按钮明确显示待真实 API 接入，不宣称复制成功。
- ConfirmDialog 实现初始焦点、Tab/Shift+Tab 循环、Escape 取消和关闭后焦点恢复。
- 玩家榜使用稳定公开项 key。

### 2.7 OBS 视觉

- 1080×1920 与 1920×1080 使用固定设计坐标和等比缩放，保持纵横比，不以裁切换取满屏。
- 安全区蒙版仅 preview 可见；主展示不输出调试层。
- 支持偏好与系统 `prefers-reduced-motion`；关键语义始终有静态文本。
- 两种尺寸检查规则、反馈、榜单、揭晓、长文本和故障画面，不推走固定主区域。

## 3. 异常与边界

- 空数据给出原因与获准下一步，不自动选择最近正式场次。
- 局部错误保留旧数据并标“已过期/采样时刻”；403 和契约错误替换相关区域并禁用依赖动作。
- 授权失效停止回执并清空公开旧画面，只显示重新授权指引。
- 命令失败不预先改写权威状态；版本冲突保留编辑草稿。
- 秘密、Cookie、CSRF 和票据仍不得进入 localStorage、URL、日志或公开 bundle。

## 4. 测试策略

按测试先行，每项修复先增加可失败测试，再实现：

1. 组件：OBS 全状态、权威时钟、空列表、共享容器、主播台矩阵、偏好、复制反馈、同名玩家。
2. 状态机/回执：重复、乱序、缺口、ready、preview、隐藏文档、旧代次、DOM 实际处理。
3. 可访问性：焦点初始位置、正反向焦点循环、Escape、焦点恢复。
4. 浏览器：八路由、独立 OBS、双尺寸、长文本、安全区、减少动画和关键故障。
5. 门禁：`pnpm test -- --run`、`pnpm build`、`PLAYWRIGHT_BROWSERS_PATH=0 pnpm test:e2e`，并扫描公开 JS/source map。

## 5. 非目标与交付说明

- 不接真实后端、不生成契约类型、不实现真实鉴权/票据/WS、不做 Windows 或 CEF 验证。
- 不更新 WP done_evidence，不宣称完整 Alpha、真实模型质量或平台验收通过。
- 最终分别列出已修复页面能力、仍属合成演练的能力、后续真实环境未验证项和实际命令结果。
