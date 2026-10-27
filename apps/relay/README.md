# WordHeat relay

公网中继持有独立 PostgreSQL 库、官方入口验签与不可变消息流、设备配对/授权、房间租约与窗口、连续 checkpoint、展示回执和独立平台履约任务。

- [可运行服务入口、迁移与私有配置](README.server.md)
- [控制面、公开平台 SDK 与可靠投递](README.control.md)
- [按原后端 Spec 的交付核对](../../docs/acceptance/20261026%20-%20后端Spec%20-%20Mac交付核对.md)

安装后使用 `wordheat-relay migrate|serve --config-root <explicit-root>`，只读该目录 `.env`。开发环境具体位置与已登记专库见根运行手册。运行/迁移角色独立；短 token 仅内存；没有官方凭据时可启动进程健康检查，相应官方能力明确不可用。可信签名、来源、房间、候选和租约校验不会由环境名或健康响应替代。

公开平台协议已提供具体适配，验签、回调批量事务、token、任务与 ACK 经过真实专库和隔离协议验证。真实官方应用/账号/回调来源和 Windows 包体仍在对应环境取证。合成 provider 与 MockTransport 保持测试身份，不产生 production 或 G0/G2 通过声明。
