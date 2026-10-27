# 共同契约开发约定

继承 [根 AGENTS](../../AGENTS.md)。适用 `packages/contracts/**`；改动同时核对两份 Spec 的 C1 附录与受影响 app 专项规则。

- WP03 将相同的 `LW-C1/1.0.0`、`schema_version=1` 快照落成 OpenAPI/JSON Schema、错误码/兼容范围/fixture/检查器。当前已有 `check_contracts.py`、Schema/OpenAPI 与测试入口；检查命令为从仓库根用 API 独立环境运行 `apps/api/.venv/bin/python -B packages/contracts/check_contracts.py --root /Users/jiachenclaw/code/wordheat-live`。检查器输出已定义子集与缺项；不能把 `valid=true` 当完整契约冻结。
- 当前主文件为 `openapi.yaml`、`relay.schema.json`、`runtime-manifest.schema.json`、`check_contracts.py`；WP26 补 `window-cases.json`。本机 WS 另有 `local-websocket.schema.json`；后端合成版本为 `LW-C1/1.0.0+LW-BE/1.1.0`，历史 C1 字节保持不变。前端生成类型去 `apps/web/src/api/generated/`，发行/模型 manifest 各留在既定目录。
- 契约包不依赖 app 实现。public/private DTO、DISPLAY/STUDIO、relay 控制帧与业务事件保持明确范围；seq/stream_seq/generation/window_epoch 在 JSON 为非负十进制字符串，不改成 JS Number。
- 保持五阶段确认、持久/渲染回执、Ready 效果授权、代次/租约/窗口/关闭屏障、可信 run_purpose 语义；变更不能通过改字段名或放宽校验静默实现。
- 先记录变更原因、影响的请求/事件/消费者、WP/验收 ID 与兼容策略，按版本同步两份 Spec、Schema/fixture、生成客户端和双方适配；只在规范内容变化后更新对应规范摘要，迁移历史摘要保持历史含义。共享文件单写入者，不手改生成类型掩盖漂移。
- 正常、重复、乱序、断线、截止、旧代次、错误角色及版本不兼容 fixture 均使用合成数据，包含应该拒绝的反例；检查两份 C1 一致、schema 有效与消费者兼容。契约通过仅证明接口组件，不代替真实 PG、模型、Windows 或平台验收。
