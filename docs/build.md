# Windows 构建与发布

## 环境与编译

构建使用 Windows x64、Python 3.10、Nuitka 和可用的 Windows C/C++ 工具链。脚本读取项目的 `.venv-build310\Scripts\python.exe`；依赖包括 Pillow、capstone、msgpack、zstandard 等，可用 `python -m pip freeze` 核对已有构建环境。

本仓库不公开开发运行配置和授权服务私钥。公开受保护客户端需要维护者提供有效运行 profile 和签名公钥，并在服务端登记对应构建 ID。

```powershell
.\build_exe.ps1 -OutputDirectory "release-v0.3.6b-local" `
  -ProtectedRelease `
  -RuntimeProfileId "<profile-id>" `
  -CapabilitySigningKeyId "<key-id>" `
  -CapabilityPublicKey "<base64-public-key>"
```

脚本从 `dps_meter.pyw` 读取版本，生成便携单文件 EXE。构建固定使用 Windows Raw Socket 和 WinDivert 2.2.2 只接收组件，验证内置二进制哈希和驱动签名，不要求 Npcap 安装器。

| 参数 | 用途 |
| --- | --- |
| `OutputDirectory` / `OutputFilename` | 输出目录和可选 EXE 文件名 |
| `BuildId` | 32 位十六进制构建 ID；省略时自动生成 |
| `RuntimeProfileId` | 对应授权服务的运行 profile |
| `ProtectedRelease` | 不打包开发运行配置，使用服务器签名租约 |
| `CapabilitySigningKeyId` / `CapabilityPublicKey` | 与服务器匹配的签名公钥信息 |
| `OfficialRelease` / `SigningCertificateThumbprint` | 独立 Authenticode 签名步骤，需要可用证书 |

受保护构建与 Authenticode 签名是独立选项。当前 GitHub `v0.3.6b` 候选包为受保护构建，EXE 未进行 Authenticode 签名。

准备正式签名包时，在上述命令中再加 `-OfficialRelease -SigningCertificateThumbprint "<代码签名证书指纹>"`。脚本会在签名后验证 EXE 的签名状态和发布者证书；本机没有可用的代码签名证书时，不能生成已签名包。签名有助于建立发布者身份，但不能保证安全软件一定放行采集驱动。

## 输出与验收

输出包括 EXE、更新日志、`update.json`、`release-manifest-<build-id>.json` 和 `build-allowlist-<build-id>.json`。

```powershell
.\.venv-build310\Scripts\python.exe tools\verify_release_package.py `
  --directory "<output-directory>" --source . `
  --version 0.3.6b --build-id "<build-id>" --backend windows_raw

.\.venv-build310\Scripts\python.exe -m unittest `
  test_startup_bootstrap test_windows_capture_startup `
  test_v030_production_boundary test_release_security `
  test_resumable_update test_update_cdn test_settings_open_flow
```

文件校验不能替代真实启动、卡号登录、采集、通关 / 团灭结算、退出及干净电脑验收。失败数量和未验收场景应记录在 Release 中；存在未完成验收时使用预发布标记。

## 发布内容

通过文件校验后生成 ZIP 和校验文件：

```powershell
.\.venv-build310\Scripts\python.exe tools\package_portable_release.py `
  --directory "<output-directory>" --source . --build-id "<build-id>"
```

- Windows 单文件 EXE：`Daodao-GMZZ-v<version>-win-x64.exe`，使用英文附件名。
- Windows 便携 ZIP：EXE、使用说明、更新日志、第三方许可证；包内保留原始中文程序名。
- SHA-256 校验文件：包含 ZIP 与独立 EXE 的摘要。
- 发布说明：更新内容、依赖、启动步骤和验收状态。

`build-allowlist` 是服务端登记材料；`update.json` 用于现有更新系统。发布 GitHub 候选包只登记新构建，不自动替换线上更新版本。

日志、卡号、数据库、私钥、`runtime-profile.dev.json`、编译目录和测试附件不包含在便携 ZIP 中。
