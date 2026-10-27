# Web 主播台与 OBS 展示

基于 React、TypeScript、Vite、TanStack Query、Zustand 与 React Router 的八页合成数据演练前端。实现依据[前端 Spec](../../docs/specs/20261025%20-%20直播语义猜词%20-%20前端开发Spec.md)。

## 当前边界

- 已实现开播准备、主播控制台、OBS 展示、题库管理、评分实验室、诊断与回放、场次复盘、设置八类页面。
- 所有业务数据来自 `src/api/fixture/` 与 `src/test/fixtures/` 的**合成 fixture**；页面上的本地命令只用于演练交互，不代表后端状态已改变。
- 真实 API、WebSocket、鉴权、connector、native 维护及票据流程尚未接入，界面会明确显示“待真实 API 接入”。
- `src/api/generated/` 保持为空，不伪造契约生成类型。后续应从共同契约生成 public/private 类型并替换 fixture 端口。
- 未执行 Windows、OBS/CEF、真实模型、真实平台或 RC 验收；组件和浏览器测试不等同于这些验收。

## 启动与构建

要求 Node.js 24+、pnpm 11+。

```bash
pnpm install
pnpm dev -- --port 5173
```

`dev` 脚本已将 CLI host/port 参数转发给 Vite。Kimi Work 预览使用项目根目录和逻辑地址 `http://localhost:7100/`。

```bash
pnpm build
pnpm preview -- --port 4173
```

## 测试

```bash
# 单元与组件测试
pnpm test -- --run

# 首次安装项目本地 Chromium（macOS/Linux 示例）
PLAYWRIGHT_BROWSERS_PATH=0 pnpm exec playwright install chromium

# 浏览器端到端测试，需先执行 pnpm build
PLAYWRIGHT_BROWSERS_PATH=0 pnpm test:e2e
```

Windows PowerShell 可使用 `$env:PLAYWRIGHT_BROWSERS_PATH='0'`，也可省略该变量并使用 Playwright 默认浏览器缓存。

浏览器测试覆盖八个路由、OBS 独立入口、1080×1920 与 1920×1080 固定画布、0.9／1／1.2 倍字号、安全区、长文本、揭晓、空轮、重连、故障、契约错误、撤权、后台共享异常场景、主播台谜底遮罩与暂停恢复、纯键盘确认框、题库筛选、诊断确认和导出前确认。它们仅验证 Chromium 中的合成页面行为，不替代 OBS/CEF、手机观看、Windows 或真实平台验收。

合成页面还支持题库分类／难度／状态筛选、300ms 搜索去抖、每页 20 条分页、完整草稿字段与未保存保护；评分失败恢复、按任务状态的回放控制和取消确认；撤票／维护确认与活动场守卫。局部查询失败会保留旧内容并标记过期，同时禁用题库写操作和复盘导出。上述命令仍只驱动明确标记的合成演练状态，只有 `applied` 结果才更新对应本地状态，不代表真实后端已应用。

## 合成场景与本机预览

- 后台页面使用 `?scenario=normal|loading|empty|partial-error|forbidden|contract-error`。
- 公开 OBS 额外支持 `preparing`、`draining`、`revealed`、`intermission`、`long-content`、`faulted`、`unauthorized`。例如 `/obs.html?scenario=revealed` 和 `/obs/session_demo?scenario=empty`。
- `obs.html` 是 preview 合成入口；`/obs/:sessionId` 在本地演练中按 primary 装配。二者均未接真实票据交换或履约接口。
- 预览偏好存入 `localStorage` 的 `wordheat-display-preferences:v0.1.0`，只含 `layout`、`fontScale`、`safeArea`、`reducedMotion`；主展示不读取这些预览偏好，也不显示安全区。
- 设置变化立即保存并作用于新的预览渲染；坏 JSON、非法布局和越界字号回退设计默认。复制一次性预览链接、票据和维护动作仍明确标记“待真实 API 接入”。

## 入口与目录

| 路径 | 职责 |
|---|---|
| `index.html`、`src/app/main.tsx` | 七个后台页面、私有路由与 `/obs/:sessionId` 路由 |
| `obs.html`、`src/app/displayMain.tsx` | 独立公开 OBS 入口，只加载公开 fixture 和公开组件 |
| `src/app/` | 应用装配、路由、导航和本地演练 store |
| `src/pages/` | setup、studio、obs、words、scoring、replay、analytics、settings |
| `src/api/fixture/` | 合成查询端口与视图模型 |
| `src/api/generated/` | 后续共同契约生成类型；当前不手写生成文件 |
| `src/features/` | 展示同步、回执、连接状态、榜单、反馈与回合计时 |
| `src/security/` | CSV 白名单序列化、公式注入防护和安全文件名 |
| `src/test/fixtures/` | 合成公开数据；不得携带真实秘密 |
| `tests/` | Playwright 页面流程；跨端安装 Alpha 测试仍属于根 `tests/e2e/` |

## 关键安全约束

- 后端拥有评分、精确命中、赢家、账本、时限与 readiness 权威；前端不从 fixture 推断真实验收结论。
- OBS 公开构建不得导入私有路由、私有谜底、题库、模型向量或平台秘密。公开答案揭晓只能来自公开 reveal 投影。
- 仅无秘密的预览偏好写入 `localStorage`；私有响应、Cookie、CSRF 和票据不得持久化。
- 外部文本只作为 React 文本节点渲染，不解析 HTML；CSV 导出前显示来源、字段白名单和脱敏确认。
- 当前轮规则冻结，设置只保存下一轮规则；版本冲突保留本机草稿。
