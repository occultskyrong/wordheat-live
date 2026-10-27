# 本机 API 开发约定

继承 [根 AGENTS](../../AGENTS.md)。适用 `apps/api/**`；实现依据 [后端 Spec](../../docs/specs/20261025%20-%20直播语义猜词%20-%20后端开发Spec.md)，跨前端行为同时读前端 Spec。

## 代码落点

`app/modules/` 固定 auth、sessions、rounds、inbox、wordbank、scoring、ranking、realtime、connectors、replays、analytics、operations。路由仅做输入/权限/响应装配，业务规则留在所属模块；数据库、时钟、队列和日志适配放 `app/infrastructure/`，outbox 用 `app/infrastructure/outbox.py`。

本机迁移放 `migrations/versions/`，已发行迁移通过追加版本演进，并同步兼容与恢复说明；测试按 `tests/{unit,contracts,integration,replay}/` 分层。不在此创建中继的 schema/迁移链，不新增重复 `modules/guesses/`，不导入其他 app 的实现替代进程接口。

## 核心约束

1. API 独占本机谜底、轮次、精确命中、积分账本与权威事件；答案/审核别名精确匹配独立于语义评分，不能以分数阈值判胜。scorer 只经内部认证接口调用。
2. `modules/connectors/` 独占本机到 relay 的主动 WSS；P-W 本机 API/scorer/PG 按 Spec 使用回环/内部接口，不为接弹幕开放家庭公网入站。平台适配不得绕过入场/轮次/账本约束直接改榜单。输入身份、来源模式和 `run_purpose` 从可信边界取得，不信浏览器声明正式用途或分数。
3. 使用真实 PostgreSQL 持久约束处理去重、唯一赢家/结算、幂等命令与事务 outbox，保持 Spec 的固定锁顺序。网络、推理和 WS 等待不进入数据库长事务；故障重试不能双计分或复活旧轮。
4. public/private DTO、DISPLAY/STUDIO 游标与角色权限分别实现；认证在后端校验，日志/公开响应不含未揭晓答案、原始 UID 或凭据。沿 Spec 实现 Cookie、CSRF、Host/Origin、票据过期/撤销及现有 WS 失效。
5. create 只核身份/用途/绑定，start 再核必要 readiness；`can_test_live` 与 `can_go_live` 分开，避免尚无 primary 或 G2 而无法创建采证场次。不能由前端、mock 或进程存活伪造 gates。
6. 事件与回执持久状态按 C1 执行：快照/重放描述不等于渲染，Ready/LIVE 授权后才处理效果；received 不释放 processed 额度。测试覆盖跨轮积压、换代、刷新与故障恢复，不跳游标掩盖缺口。
7. 开发根 `.env` 的运行/迁移连接按角色最小字段加载，权限不混用；Windows 发行运行凭据用 DPAPI，引导/boot 密钥按原生 IPC 机制。缺配置/模型/身份时明确失败，不回退公共密码、trust 或假分。

## 迭代与验证

WP05 数据基础，WP07–12 核心和事件，WP16 诊断/回放/统计，WP25–28 接双端；先核实际 `depends_on`。首轮完成当前组件的真实持久层/接口验收，随后在 WP21、WP27 等完成跨工程闭环，不反向等待完整安装器才能测 schema/事务。

契约、输入幂等、判胜/竞态、事务回滚、公私权限、回执恢复及模式隔离各按受影响用例验证。需要持久语义时用专用真实 PG，不能用 SQLite/内存测试替代；故障测试只操作核实归属的测试实例/自有进程。同步接口/迁移/错误码与脱敏证据，完成标准以本 WP 为准。
