# Windows 原生交付

状态：仅目录预留，不含可执行 PowerShell、安装器或已签名产物。

| 工作包 | 未来实现 |
|---|---|
| WP04 | build.ps1、Windows x64 构建链、依赖锁定与 CI |
| WP19 | database.ps1、专用 PG/服务账号、SID/DPAPI 配合引导 |
| WP20 | installer.iss、verify.ps1、离线模型包与基础 Alpha |
| WP29 | platform-package.ps1、平台认可的正式入口包体 |
| WP31 | upgrade.ps1、联合版本切换、维护与受控恢复 |

安装、验证与恢复说明落 `docs/windows/`，真实测试落 `tests/windows/`。Program Files/ProgramData/LocalAppData 布局、ACL、Job、迁移兼容矩阵和卸载保留数据按后端 Spec 第12节执行。禁止打包 `.env`、签名私钥、真实用户数据或本机开发缓存。
