# WordHeat Live · 项目开发约定

适用于整个仓库；进入具体目录后同时遵守其下的 `AGENTS.md`。本文件维护项目入口、目录归属和开发边界，调度、工具与通用 Git 流程沿用当前宿主规则。规则建立本身不启动业务开发、安装、平台试验或部署。

## 开工入口

1. 核对当前 checkout、分支、状态与本次授权范围，保留已有改动；不自行切换工作区、pull、stash、提交或推送。
2. 读取 [工程目录规范](docs/architecture/20261025%20-%20词温项目%20-%20工程目录与迭代规范.md) 与 [敏感信息规范](docs/security/20261025%20-%20词温项目%20-%20敏感信息与配置规范.md)，再读改动范围的专项规则和前/后端 Spec。
3. 从 [工作包清单](docs/plans/20261025%20-%20直播语义猜词%20-%20开发任务清单.json) 确定 WP、`depends_on`、`planned_files`、`component_verification`、`done_evidence` 和验收 ID，结合 [Windows 开发计划](docs/plans/20261025%20-%20直播语义猜词%20-%20Windows开发计划.md) 执行。
4. 完整接口行为以 [前端 Spec](docs/specs/20261025%20-%20直播语义猜词%20-%20前端开发Spec.md)、[后端 Spec](docs/specs/20261025%20-%20直播语义猜词%20-%20后端开发Spec.md) 及二者相同的 `LW-C1/1.0.0` 快照为准。WP03 将其落成可执行契约；目录约定不覆盖协议。

后端已有 API、scorer、relay 组件及独立依赖锁、配置加载、开发启动/测试入口；从根 README、各服务 README 和 [后端执行计划](docs/plans/20261025%20-%20后端开发迭代%20-%20执行计划.md) 定位。按实际文件与当前证据确认范围，不从入口说明推定整体验收通过。清单中的 `execution_authorized` 是规划时快照，不能替代当前会话授权，也不要求已获授权的正常步骤再次确认；不得据此授权一次性实施全部工作包。

## 目录与责任

| 路径 | 责任与专项入口 |
|---|---|
| `apps/web/` | 八类页面、studio/OBS 投影与交互；读 [Web AGENTS](apps/web/AGENTS.md) |
| `apps/api/` | 本机谜底、回合、积分、事件与唯一 relay 连接；读 [API AGENTS](apps/api/AGENTS.md) |
| `apps/scorer/` | 独立真实推理，只编码；读 [scorer AGENTS](apps/scorer/AGENTS.md) |
| `apps/relay/` | 官方入口、独立持久库、租约/窗口/投递/履约；读 [relay AGENTS](apps/relay/AGENTS.md) |
| `apps/windows-launcher/` | 原生进程、身份、启动与维护；读 [Launcher AGENTS](apps/windows-launcher/AGENTS.md) |
| `packages/contracts/` | 共享 OpenAPI/Schema/fixture 与生成契约；读 [contracts AGENTS](packages/contracts/AGENTS.md) |
| `packages/evaluation/`、`resources/` | 模型评测、授权题库/校准与唯一模型清单；不承载运行数据库或未脱敏材料 |
| `deploy/windows-native/`、`deploy/public-relay/`、`deploy/release/` | 安装/部署/恢复/发布材料；业务实现留在 apps |
| `tests/` | 跨工程、Windows、负载与故障测试；各 app 保留自己的组件/单元/集成测试 |
| `spikes/`、`docs/feasibility/` | 平台与 Windows 风险试验及实际证据；原型不直接成为生产依赖 |
| `docs/acceptance/`、`docs/operator/` | 脱敏验收记录和使用手册；方案/Spec/研究证据分别留在已有目录 |

路径差异沿用目录规范中的既定处理：`apps/api/app/infrastructure/outbox.py` 不建同名目录；API 不另建 `modules/guesses/`；`resources/model-manifest.json` 只有一份；P-W 首发不扩建 `deploy/remote-core/`。新增目录或职责变化应同步目录说明与相关 WP，避免并行实现同一职责。

## 开发迭代方式

- 首发范围沿用 F01–F60、W01–W18；P1/P2 和集中式 P-A 不因基础建设自动纳入。真实依赖以工作包清单为准，里程碑不是全部串行开工指令。
- 先 WP01/WP02 风险证据，再 WP03 契约、WP04 依赖锁/Windows 构建，随后 WP05/WP06 数据与前端基础。后续沿 M2 内核→M3 八页离线 Alpha→M4 中继 Beta→M5 恢复/RC→M6 真实直播推进；组件交付按本 WP 的包内验收，不倒置依赖等待后续 Alpha/RC。
- 前后端先对齐契约和冻结的合成 fixture，再各自实现组件、接真实 API、联调正常/拒绝/失败/恢复路径。fixture 必须标记演练，不据其结果宣称接通或验收通过。
- 每轮明确 WP、改动路径、受影响契约与验收点；在授权范围内完成一个可验证增量，发现影响范围外的问题先记录影响。共享契约、迁移链、依赖锁和生成物由一个写入者串行维护；可并行的实现必须避开共享写集。
- `packages/contracts` 不依赖 apps；应用通过明确接口交互，不互相导入另一个服务的实现。API 适配器→应用用例→领域规则→存储/推理接口；Web 通过本机 API 消费公开/私有投影。
- API/scorer/relay 的依赖、锁与真实测试命令已写入各工程入口；后续构建/Windows 交付按 WP03–06 同步各工程 README、`docs/build.md` 与 CI。使用锁文件与冻结模型版本，不在发行运行时安装依赖或补下载模型。当前不存在的命令只能列为待实现，不能记录为执行通过。
- 改动接口先走 contracts 专项规则；改动产品行为同步相应 Spec、用例与文档。纯目录/AGENTS/文档改动做路径、链接和一致性检查即可，不因此启动产品服务。

## 验证与完成标准

按改动风险执行相关层级：契约/组件→真实 PG 事务或模型→跨端→Windows/OBS→固定 RC 真实平台。选择能证明当前改动的测试，不给每次小改新增全量 51 项或统一覆盖率门槛。没有环境或依赖时明确未验证点。

组件完成与整机/平台放行分别记录。M3/WP21 要重建含全部八页的 Alpha；M4 正式中继实现依原计划前置；WP31–33 先做预候选恢复/故障验证，WP34 再冻结 RC，WP35 用固定候选采 G2，不能循环等待未来产物。G0、G1-Q、G1-R、G2 与 readiness 的用途保持 Spec 定义。

交付说明至少写明变更目的/范围、实际执行的命令与结果、未验证项、关联 WP/验收 ID 和脱敏证据位置。工作包状态只有满足自身 `done_evidence` 后才能更新；不因目录、mock、单元测试或构建成功而把真实平台/Windows 验收标为通过。

## 配置与执行边界

- 开发 API 使用根 `.env`，中继使用 `deploy/public-relay/.env`；配置按进程最小字段读取，仅空值 `.env.example` 可提交。秘密不进入源码、文档、fixture、日志、前端包或发行包。Windows 长期凭据用 DPAPI/ACL，boot 密钥仅受限 IPC/内存。
- 开发数据库数据、权重、日志、备份与原始证据放私有存储或被忽略的根 `.local/`；真实 Windows 安装目录按 Known Folders 定位。测试必须确认专用实例及命名空间，禁止借用其他项目或生产环境的数据库和凭据。
- 安装器与模型包按 manifest 白名单收集；运行数据库、用户数据、密钥与私有 `.env` 不能递归带入。运行数据删除、恢复与外部资源操作遵守当前明确授权。
- 平台/云部署与真实直播按当时权限执行；禁止服务器 SSH 类访问，部署使用获授权云控制台/CI。仅用户授权的 Git 同步可使用已配置 Git SSH remote，此例外不构成服务器登录授权。
