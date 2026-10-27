# Web 开发约定

继承 [根 AGENTS](../../AGENTS.md)。适用 `apps/web/**`；实现前读 [前端 Spec](../../docs/specs/20261025%20-%20直播语义猜词%20-%20前端开发Spec.md)。技术沿用 React/TypeScript/Vite、TanStack Query、Zustand 与原生 WebSocket，依赖版本在 WP04 锁定。

## 代码落点

- `src/app/` 装配入口/路由/登录与错误边界；`index.html`/studio 和 `obs.html`/displayMain 独立构建。
- `src/pages/{setup,studio,obs,words,scoring,replay,analytics,settings}/` 对应八类页面；功能逻辑放 `src/features/`，共用 UI/图表放 `src/components/{ui,charts}/`。
- `src/api/` 处理角色客户端、查询、事件解码和分开的 studio/display 流；`src/api/generated/{public,private}.ts` 从共同契约生成，不手写另一套漂移 DTO。
- `src/stores/` 保持 studio/display 独立投影，`src/styles/` 放样式，`src/security/` 放安全文本/CSV/脱敏辅助；合成 fixture 放 `src/test/fixtures/`。
- 状态机单元测试邻近功能实现，浏览器流程放 `tests/`；安装 Alpha 跨端测试在根 `tests/e2e/`。

## 实现边界

1. OBS 构建依赖只允许公开类型、display API、公开 store 与组件，不能导入 studio router、private 客户端、题库或向量。检查 chunk、source map 与 fixture 的依赖/字段白名单；类型同目录不代表可互导。
2. 后端拥有分数、精确命中、赢家、账本、时限与 readiness 权威；前端不计算/猜测这些业务结果，不加载模型/邻居词库，也不从状态名或进程存活推断可正式开播。
3. 服务端查询状态交给 TanStack Query；连续 DISPLAY/STUDIO 投影分别进入对应 store；草稿/弹窗为局部状态。只有无秘密的偏好可本机持久化；Cookie 由浏览器处理，CSRF/票据仅短命内存，获授权的私有响应可进入身份隔离的内存查询缓存并在登出/切身份时清空，均不得写 localStorage/IndexedDB 等持久缓存。
4. 角色 Cookie、精确 Origin/Host 与 CSRF 按 Spec 配套实现；登录和展示兑换是两个指定 bootstrap 例外，不能扩成所有 POST 豁免。展示 ticket 只短暂取自 fragment、立即清除并交换，浏览器 WS 不在 query 传 token。
5. 快照→顺序恢复→Ready 授权→真实渲染回执按 Spec 执行；两个 audience 不共用游标。恢复快照、收到事件或 replay 不能冒充本次 processed，primary/render_generation 由后端授予。
6. 所有外部文本作为 React 文本节点；CSV 用字段白名单与公式注入防护。秘密不能进入 `VITE_`、define、静态 JSON、HTML、map 或 OBS URL；前端不读取根/中继私有 `.env`。

## 迭代与验证

WP06 建基础；WP13–15、WP17 实现页面；WP30 接中继真实故障状态，实际依赖仍看清单。先使用冻结合成 fixture 验证 UI/解码与错误态，再接真实 API；两类状态必须可辨别。

相关变更验证角色/公私隔离、刷新重连、重复/乱序、版本冲突、任务 pending/失败、空数据/加载态及真实展示回执；视觉改动验证 OBS 目标尺寸、安全区、键盘/对比与 CEF 实际兼容。组件测试不替代 Windows/OBS 实测。目录和 fixture 完成不等于全部八页 Alpha 接线完成。
