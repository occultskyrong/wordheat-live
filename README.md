# 词温 · WordHeat Live

中文语义猜词直播互动系统，采用Windows原生本地部署＋轻量公网中继。

仓库包含方案、原始 Spec 和应用工程。本机后端提供真实离线模型、题库校准审核发布、持久选题与多轮猜词、回放、冻结统计与 CSV、中继可靠传输、原生维护及独立恢复流程。当前交付按原后端 Spec 章节、BE-A 与 T/WT 逐项核对；Windows 原生包体、OBS 和官方授权链路由实际环境取证。

## 从这里开始

- [资料导航](docs/README.md)
- [项目开发约定](AGENTS.md)（进入各应用后继续读取本目录 AGENTS）
- [工程目录与迭代规范](docs/architecture/20261025%20-%20词温项目%20-%20工程目录与迭代规范.md)
- [敏感信息与配置规范](docs/security/20261025%20-%20词温项目%20-%20敏感信息与配置规范.md)
- [解决方案v1.1](docs/solution/20261025%20-%20直播语义猜词%20-%20落地解决方案.md) · [离线HTML阅读版](docs/solution/20261025%20-%20直播语义猜词%20-%20方案阅读版.html)
- [前端开发Spec](docs/specs/20261025%20-%20直播语义猜词%20-%20前端开发Spec.md)
- [后端开发Spec](docs/specs/20261025%20-%20直播语义猜词%20-%20后端开发Spec.md)
- [Windows开发计划](docs/plans/20261025%20-%20直播语义猜词%20-%20Windows开发计划.md) · [36个工作包清单](docs/plans/20261025%20-%20直播语义猜词%20-%20开发任务清单.json)

## 目录

```text
apps/
  api/                本机业务与数据库迁移
  web/                主播台、OBS展示与八类页面
  scorer/             独立模型推理
  relay/              公网持久中继与平台履约
  windows-launcher/   Windows原生启动器
packages/
  contracts/          共同接口、事件和发行契约
  evaluation/         模型评测与标注工具
resources/wordbank/   授权题库、校准与后续资源清单
deploy/
  windows-native/     原生构建、安装、PG与升级材料
  public-relay/       中继私有配置与部署材料
  release/            发布候选清单
spikes/               平台与Windows可行性试验
tests/                跨端、Windows、负载与故障验证
docs/                 方案、Spec、计划、研究、架构与验收资料
scripts/docs/         现有HTML阅读版构建器
```

各工程 README 说明职责与入口；`.gitkeep` 仅保留空目录。实现、测试与外部验收以实际代码和绑定当前版本的证据为准，不从目录存在推定工作包完成。

迭代沿用M0可行性→M1契约/工程基础→M2核心→M3离线Alpha→M4中继Beta→M5发布候选→M6真实交付，具体依赖以开发任务清单为准。API、scorer、relay 各有 `pyproject.toml`、`uv.lock` 和独立 `.venv`；Windows 构建与安装器仍按相应工作包验收。

## 后端开发入口

- [API 与数据库](apps/api/README.md) · [Mac/Linux 开发启动机制](apps/api/README.devruntime.md)
- [离线 scorer](apps/scorer/README.md) · [relay 协议组件及可信适配边界](apps/relay/README.md)
- [原后端 Spec 逐项交付核对](docs/acceptance/20261026%20-%20后端Spec%20-%20Mac交付核对.md)
- [Mac 后端运行与维护](docs/operator/20261026%20-%20Mac后端%20-%20运行与维护手册.md) · [完整装配/配置说明](docs/build.md)
- [后端执行计划](docs/plans/20261025%20-%20后端开发迭代%20-%20执行计划.md) · [历史验证资料](docs/acceptance/README.md)

本机容量按实测声明：**2 个新独立词/秒 × 1800 秒零拒绝**；4 词/秒为实测临界（30 分钟出现显式 429 与 RESULT_STALE），6/10 词/秒不支持，原 Spec 的 10 词/秒目标已明确缩减。失败报告原样保留，容量数字与环境限制见交付核对的容量附录；Windows 目标须按 WT08 另行复测。

当前后端契约由实际三应用导出并校验：**65 个 HTTP 操作 + 3 个 WebSocket 协议**，C1 基线 45 项全定义，`check_contracts.py` 报告 `valid=true` 且无未定义操作。审计出的实现缺口（会话撤销与 `disabled_at`/`guesses.epoch` 字段、official 时延采样、6 秒失联与 30 秒传输预算覆盖、短码耗尽判定、relay 组件测试装载）已于 2026-10-27 全部关闭，明细见[缺口登记与处置](docs/acceptance/20261026%20-%20后端Spec%20-%20缺口登记与处置.md)。

历史组件计数和派生清单保留为旧轮诊断；它们不定义本轮开发进度或替代原 Spec 验收。

本机仓库为 `/Users/jiachenclaw/code/wordheat-live`。Mac/Linux 开发工具使用两个独立解释器启动 API/scorer；先校验模型并预热，再建立 API 依赖和健康观测。运行前需已有专库迁移、私有配置和模型文件；命令不安装依赖、下载模型或管理数据库。

```sh
cd /Users/jiachenclaw/code/wordheat-live
apps/api/.venv/bin/python -B scripts/backend/20261025-run-local.py \
  --root /Users/jiachenclaw/code/wordheat-live \
  --config-root /Users/jiachenclaw/code/wordheat-live \
  --api-python /Users/jiachenclaw/code/wordheat-live/apps/api/.venv/bin/python \
  --scorer-python /Users/jiachenclaw/code/wordheat-live/apps/scorer/.venv/bin/python \
  --manifest /Users/jiachenclaw/code/wordheat-live/resources/model-manifest.json \
  --model-root /Users/jiachenclaw/code/wordheat-live/.local/models/bge-small-zh-v1.5
```

API 监听 `127.0.0.1:8765`，scorer 监听 `127.0.0.1:8124`。该工具仅管理自己创建的两个子进程，不证明 Windows Launcher、SID/ACL、DPAPI、Job Object 或 OBS 验收。relay 已实现公开平台协议适配及受信控制面，使用独立配置/数据库和固定入口；缺少真实授权时保持相应能力不可用。其配置和运行入口见 relay 服务说明。

纯本地契约与工具验证：

```sh
cd /Users/jiachenclaw/code/wordheat-live
apps/api/.venv/bin/python -B -m pytest -q packages/contracts/tests scripts/backend/tests
apps/api/.venv/bin/python -B packages/contracts/check_contracts.py --root /Users/jiachenclaw/code/wordheat-live
```

PG/模型/relay 分别用所属服务 `.venv`，逐服务测试；API pytest 为每个进程建立随机自有 schema，并仅清理自有 schema/精确标记的 fixture；仍使用已分配专库并串行运行。relay 按自己的 fixture 边界执行。具体命令和证据规则见后端执行计划。契约校验、源码绑定和实际业务执行分别提供证据；完整接口定义不能替代 Windows、平台或发布验收。

## 私有配置

本地开发使用根目录 `.env`，公网中继使用 `deploy/public-relay/.env`，均已加入 Git 忽略。只提交空值 `.env.example`。API 与 relay 已有按字段白名单加载的配置入口，运行/迁移角色分离；私有文件在本机使用 0600 权限。Windows 发行版凭据继续采用 DPAPI，启动密钥保存在内存，不随安装包分发 `.env`。具体字段、权限、前端隔离和验收要求见敏感信息与配置规范。

## 重建离线阅读版

HTML可直接打开。重建需要已有Node.js与marked模块：

```sh
node scripts/docs/20261025-build-solution.cjs
```

构建器从脚本位置解析仓库路径，不要求固定cwd；可用MARKED_MODULE指定已安装的marked，且不会现场下载或安装依赖。输出位于docs/solution，完整内嵌正文、四份研究附录和六幅SVG。

本次迁移保留原资料命名与技术版本，仅修复仓库相对路径和来源摘要。原始与迁移后摘要见docs/research中的迁移清单；这不是开发或环境验收通过记录。
