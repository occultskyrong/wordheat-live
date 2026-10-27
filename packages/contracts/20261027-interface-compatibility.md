# 2026-10-27 后端接口兼容说明

范围仅为四项已确认接口修复，基于本次 `0f571221…42dfd18` 源码快照。历史 `LW-C1/1.0.0` 原始字节及后端 Spec 摘要保持不变；当前后端组合 schema 反映以下行为。

| 修复 | 当前行为与兼容边界 |
| --- | --- |
| 当前登录退出 | `DELETE /api/v1/auth/sessions/current` 执行当前 Cookie 会话的 logout，并清除 Cookie；静态路径优先于已有 `/auth/sessions/{session_id}` 撤销路径。请求和响应格式不变。 |
| CSV 确认导入结果 | 命令 `state` 增加明确类型 `{validation_id, operation, puzzle_version_ids}`；`operation` 为 `confirm_valid_rows` 或 `cancel`。确认响应、幂等重试、GET command 与 GET import report 返回同一冻结 IDs。ImportResult 的 `puzzle_version_ids` 必返；未确认报告及取消结果为 `[]`。旧请求 `confirm` 仍接受并规范化为 `confirm_valid_rows`，不自动发布草稿。 |
| WS 展示回执 | `/relay/v1/streams` 的 `display.received` / `display.processed` 需要在握手头协商 `X-BE-Extension-Version: LW-BE/1.1.0`，帧为 `{kind, stream_seq, receipt_id, client_time}`。时间必须为真实字符串 `YYYY-MM-DDTHH:mm:ss.sssZ`，relay 不补当前时间。`resume` 与 `transport.persisted` 维持原帧与确认行为；BE 版本不放进 JSON 帧。 |
| 正式连接公开投影 | official 的 DISPLAY/STUDIO 快照、heartbeat 与 `connection.status` 事件投影真实连接状态。公开 transport 仍只有 C1 五态及 `{state, reason_code, recoverable, last_confirmed_at}` 四字段；不输出内部认证状态、授权资料或凭据。本地演练继续使用对应本地状态原因。 |

WS 缺少/不支持 BE 返回 `relay.error`，`reason_code=BE_EXTENSION_VERSION_UNSUPPORTED`；已协商但缺少/非法时间为 `INPUT_INVALID`。同 stage/receipt 的实际 SDK 毫秒不同，或历史履约 job 没有真实时间，为 `RECEIPT_CONFLICT`。这些错误以关闭码 `1008` 拒绝，不新增 stage/job，不以假 `pending` 声称已受理；既有已提交事实保留。

HTTP `/relay/v1/delivery-stages` 保持原日期格式兼容，包括既有时区偏移形式，标准本机 HTTP 回执路径不受此次 WS 修复影响。HTTP/WS 共用原 outbox identity；同一时刻按 UTC 毫秒判等，不覆盖已存 HTTP 原始时间字符串，不产生重复履约任务。

`relay.schema.json` 的 `ClientControl` 定义实际帧，`ClientControlExchange` 是将握手头与帧一起校验的组合视图，并非新增线上 wrapper。头名在应用此视图前规范到所示大小写。正反例均为合成数据；动态重复/历史 job 冲突的持久语义由 relay 定点回归核实，静态 schema 不冒充数据库或平台执行证据。本次不新增前端生成物、性能结论或平台放行。

时间 schema 保留标准 `format=date-time` 与 WS 毫秒 Z 形状 pattern。Draft 2020-12 默认将 format 作为注解；合同回归显式注册标准库 datetime 的 FormatChecker 验证日历日期，不依赖可选格式插件。真实 WS 对非法日历、负 SDK epoch 等语义的拒绝由 relay 的实际入口回归负责；HTTP 原解析与兼容范围不变。
