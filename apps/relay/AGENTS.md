# Relay 开发约定

继承 [根 AGENTS](../../AGENTS.md)。适用 `apps/relay/**`；读后端 Spec 第08节、对应 C1 帧及相关 WP。G0 确认平台真实字段/验签/反馈形态，不照搬来源未核实的 SDK 行为。

- `app/` 实现 ingress、pairing/leases、streams/checkpoints、windows、platform/fulfillment；`migrations/`、`tests/` 分别维护中继独立持久库和验证。本机迁移和业务账本不复制到此。
- WP22–24 入口/所有权/可靠投递，WP26 窗口，WP27 双端联调，WP28 官方适配与履约；遵循原计划前置，WP22 依 WP21，不能把冻结 fixture 或目录称为正式平台实现。
- 官方输入验签、去重、持久化后才给对应持久确认；保持至少一次传输与幂等效果。relay 接收、本机持久 ACK、display received/processed、平台履约分别保存证据，不能互相代替。
- app/room 正式 owner 唯一；原 owner_generation 不可变，当前 delivery_generation/connection_nonce 授权与窗口 epoch 分开校验。连续游标/gap/关闭屏障/可信时间按 Spec，不能跳水位、复用旧代次或补算旧轮。
- relay 不存指定谜底映射、题库向量、校准或本机积分权威；观众评论可能包含答案字面，不因此建立服务端答案映射。保留期/配额清理遵守幂等和确认依赖，不能只因载荷到期删必要元数据。
- 私有配置取 `deploy/public-relay/.env` 或受控云变量，只给所需进程字段；数据库与本机独立，平台秘密不下发 Windows/Web。部署经授权云控制台/CI，禁止服务器 SSH、远程 shell 和任意下载执行。
- 相关验证使用专用真实 PG/WS：持久化边界崩溃、重投/ACK 丢失、同序号改内容、owner 竞争、旧窗口、gap、重连和履约失败。真实平台验收留给授权测试与固定 RC 的 G2，不能以 mock 取代。
