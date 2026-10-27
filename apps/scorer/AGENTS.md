# Scorer 开发约定

继承 [根 AGENTS](../../AGENTS.md)。适用 `apps/scorer/**`；读后端 Spec 的真实模型、内部接口和 Windows 交付章节。

- 模型 loader、encoder、batcher、cache、health 放 `app/`，验证放 `tests/`。WP04 落依赖/冻结入口，WP08 实现真实编码及 G1-Q；`packages/evaluation/` 承担评测工具。
- 仅提供 Spec 定义的内部 embeddings 接口，只编码，不持有指定题目的答案映射、裁定赢家或操作积分；不能暴露为浏览器/公网评分服务。
- 固定模型仓库/revision、tokenizer、pooling、normalize、精度与维度；消费唯一 `resources/model-manifest.json`。离线校验与预热后才能就绪；缺文件、摘要/维度不符或 NaN/Inf 明确失败，不下载补齐或给 mock 分。
- 单模型实例加载一次，批量/线程/缓存受容量约束；首版 CPU，与 OBS 资源竞争按 G1-R 实机验证，不默认占用 OBS 编码 GPU。
- 内部认证按本次 boot 的受限 IPC/内存密钥执行，请求/响应身份与重放检查按 Spec；密钥不写 `.env`、命令行或磁盘明文。
- 测试固定 manifest 的真实编码、批处理/超时、异常向量、预热失败及认证；G1-Q 质量和 G1-R Windows/OBS 资源分别取证。模型包、词库、校准版本联合冻结，组件 mock 不能用于模型验收。
