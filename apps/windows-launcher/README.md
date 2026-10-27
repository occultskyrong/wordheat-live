# Windows 原生启动器

状态：仅目录骨架。按后端 Spec 保留根层模块布局，后续在这里实现 process_host、state、health_client、bootstrap、credentials、maintenance 和 platform_entry；不另嵌套第二个 app/ 工程。

- WP18：原生窗口/托盘、状态机、进程 Job 与身份健康。
- WP19：专用 PG、目标 SID 与 DPAPI 安全引导；安装脚本在 `deploy/windows-native/`。
- WP25：协调 API 的中继连接状态；WSS 消费者仍唯一位于 API `modules/connectors/`。
- WP29：平台认可 EXE 入口；WP31：维护、升级和恢复。
- `tests/`：生命周期、bootstrap、identity；真实 OS、SID/ACL、Job 和恢复在根 `tests/windows/` 验证。

首版沿用 Python ctypes/Win32 原生窗口及 PyInstaller onedir，不引入 Electron、Qt 或必装 WebView2。运行凭据使用用户级 DPAPI 与 ACL；boot 密钥在受限 IPC/内存，不放 `.env` 或命令行。Program Files/ProgramData/LocalAppData 是安装运行位置，不在源码仓库建立数据副本。当前没有可执行启动命令。
