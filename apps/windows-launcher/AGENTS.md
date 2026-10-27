# Windows Launcher 开发约定

继承 [根 AGENTS](../../AGENTS.md)。适用 `apps/windows-launcher/**`；实现依据后端 Spec 第12节及 Windows 工作包。

- 根层实现 process_host、state、health_client、bootstrap、credentials、maintenance、platform_entry；测试放 `tests/`，不再嵌套 `app/`。构建/安装/服务脚本在 `deploy/windows-native/`；目标机测试在根 `tests/windows/`。
- 首版 Python ctypes/Win32 原生窗口/托盘＋PyInstaller onedir，角色独立入口；不把冻结 EXE 当解释器调用 `-m`。不添加 Qt/Electron 或必装 WebView2 前置，依赖与 Windows x64 构建在 WP04 锁定。
- 原生窗口能在 API 启动前显示阶段/失败/重试；身份健康核对 PID/SID、安装实例、boot 与发行摘要，不能只看端口存活。readiness 由后端计算，不在托盘自行放行直播。
- Job 只管理本产品自有 API/scorer，按 Spec 受限继承及退出；已有 PG 服务和 OBS 不加入 Job、不随关闭误杀。WSS 消费唯一属于 API connectors，Launcher 不另建连接。
- 用 Known Folders、明确实例归属/目录 ACL 和重解析点校验；UAC 维护身份与实际运行用户 SID 分开。运行凭据通过受控一次性引导交给目标用户并用 DPAPI 保护，运行用户不能得到迁移凭据；boot 密钥只走受限句柄/管道和内存。
- 维护只接受 Spec 的固定 operation，本机向导确认与必要 UAC 不得被 Web 参数替代；不接任意 path/executable/URL。先维护态/备份/兼容验证再切换，不兼容恢复到独立实例；卸载默认保留用户数据。
- WP18/19/20 启动/引导/Alpha，WP25 配合连接状态，WP29 平台 EXE，WP31 升级恢复；实际依赖看清单。最小安装 Alpha 与含完整八页的 WP21 Alpha 分别验收。
- 单元/假进程验证后，SID/ACL、DPAPI、Job、spawn/DLL、跨管理员安装、升级/恢复与 OBS 必须在真实 Windows 取证；macOS/Linux 的检查不能标为 Windows 验收通过。G1-R、平台认可包体与 G2 分别保留证据。
