# WordHeat 本机业务 API

完整装配入口为 `app.application.create_app`；`app.main.create_app` 是核心组件入口。标准进程负责会话/权限、持久题库与评测、预选和回合、真实评分任务、积分账本、两类快照与 WS/effects、回放、冻结统计和 CSV、中继连接、原生维护及自动保留期清理。

- [完整配置与后台生命周期](../../docs/build.md)
- [当前 Mac 运行与维护](../../docs/operator/20261026%20-%20Mac后端%20-%20运行与维护手册.md)
- [按原后端 Spec 的逐项核对](../../docs/acceptance/20261026%20-%20后端Spec%20-%20Mac交付核对.md)
- [统计实现边界](README.analytics.md) · [开发进程引导](README.devruntime.md)

与 scorer、relay 使用独立锁定环境；业务运行角色不执行 DDL。迁移由显式迁移命令或经过原生确认的固定安装包迁移阶段使用已登记迁移角色执行。网络、推理和 WS 等待在业务事务之外，版本、boot、lease/epoch和输入hash在结果提交时重新验证。

本地演练不需要官方密钥。原生控制仅接受固定动作与登记产物，HTTP 请求不代表维护已执行。生产模式由真实签名授权/门禁、正确房间、完整候选、租约和 primary 放行。Schema 完整、组件通过和 Mac 协议负载均不替代 Windows/OBS/官方实机验收。

真实 PostgreSQL 测试按每个进程的自有随机 schema 执行，只清理自有资源。持续容量测试必须显式启用并提供专库、真实模型与独立解释器；不能用测试数量或派生清单替代原 Spec 交付。
