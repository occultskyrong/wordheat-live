# 前端视觉与交互精修实施计划

对应设计：`20261027-visual-interaction-refinement-design.md`。所有修改限于 `apps/web/**`。

## 1. 建立回归约束

- 在 Playwright 增加 390×844 主播台检查：四项指标两列、页面无横向溢出、回合操作更早出现。
- 增加移动题库检查：搜索整行、筛选两列、只读字段具有区别于可编辑字段的计算样式。
- 增加移动导航检查：初始存在右侧可滚动提示，滚到底后提示消失；所有页面入口仍可到达。
- 先运行新增测试并记录预期失败，再实现。

## 2. 第一轮实现

- `src/styles/tokens.css`：仅依据对比度计算调整次级文字色。
- `src/styles/studio.css`：小屏双列指标、紧凑页眉与题库筛选、只读态、44px 主要触控目标和导航边缘提示。
- `src/pages/studio/StudioPage.tsx`：只添加布局语义类，不改命令逻辑。
- `src/pages/words/WordsPage.tsx`：只添加筛选布局语义类，不改草稿、筛选或版本逻辑。
- `src/app/AppShell.tsx`：用 ref、滚动事件和 ResizeObserver 管理真实溢出边界；路由变化时让当前导航可见，尊重减少动画。

## 3. 同尺寸视觉复审

- 复用 `.local/visual-audit.mjs`，保留 1440×1000 和 390×844 的审查尺寸。
- 对比首轮截图，检查主播台与题库首屏改进以及桌面回归。
- 扩展采集设置、评分、回放、复盘和确认弹窗的移动状态。
- 仅对有截图、计算样式或交互证据支持的问题继续迭代。

## 4. 完成验证

依次执行：

```bash
pnpm test -- --run
pnpm build
PLAYWRIGHT_BROWSERS_PATH=0 pnpm test:e2e
```

检查 OBS 两个入口及 source map 不包含私有字段，确认没有遗留 Vite、Node 或 Playwright 服务。交付时说明实际结果与未覆盖的 Windows／OBS CEF／真实平台验收。
