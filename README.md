# 叨叨诡秘助手

Windows 游戏战斗统计工具。当前开发源码保留悬浮 HUD、队伍 DPS / HPS / 承伤、首领信息和战斗记录采集；战绩查看使用网页。

**当前版本：v0.4.2** · 客户端构建 `0.4.2+20261030.3`。

主窗口使用精简悬浮 HUD，设置左侧提供更新按钮，更新信息直接显示在 HUD 内。

[下载 Windows 便携包](https://github.com/cblan23/gmzzdps/releases/tag/v0.4.2) · [使用指南](docs/usage.md) · [常见问题](使用问题大全.md) · [构建与发布](docs/build.md) · [验证状态](docs/release-v0.4.2.md)

## 快速开始

1. 在 [Releases](https://github.com/cblan23/gmzzdps/releases/tag/v0.4.2) 下载 `Daodao-GMZZ-v0.4.2-win-x64.zip`，解压到可写的文件夹。
2. 启动游戏，进入角色可操作界面；建议在进入副本前启动助手。
3. 双击 `叨叨诡秘助手-v0.4.2.exe`，允许管理员权限，在登录页输入有效卡号。
4. 连接游戏后开始战斗。底栏齿轮打开设置，`Home` 默认显示 / 隐藏主窗口。

便携包自带运行依赖，**不需要安装 Python 或 Npcap**。GitHub 自动生成的 `Source code` 压缩包是源码，不能直接替代 Windows 程序包。

## v0.4.2 更新

- 新增五月庄园·城堡困难模式三场 Boss 的 8 分钟狂暴预测。
- 一号信徒的安西娅、巴尼先生和后继安西娅按同一场战斗处理；子爵夫人与神话姿态共享战斗和狂暴时钟。
- 修复双 Boss 血量消息交替时实时 DPS 列表反复清空的问题，并在短暂不完整快照期间保留本场最后一份完整实时行。
- 实时 DPS 只显示当前战斗；上一只 Boss 和上一场战斗的结果仍保留在战斗记录，不再回显到实时窗口。
- 网页入口、困难模式 Boss 资料、上传归类和公开查询已同步到当前副本。

详见 [更新日志](release-notes-v0.4.2.txt)。

## 功能

| 功能 | 内容 |
| --- | --- |
| 主窗口 | 透明悬浮 HUD、职业与玩家信息、统计数值、总量、死亡次数、战斗时间、锁定和置顶 |
| HUD 设置 | 显示、外观与快捷键设置；显示当前卡号到期时间 |
| 首领信息 | 血量、首领识别、已有资料支持的狂暴节奏预测 |
| 战斗记录 | 本地保存和自动上传；从 HUD 打开网页查看详细记录 |
| 登录与更新 | 卡号登录、本机加密记忆、后台检查更新、下载大小与 SHA-256 校验 |

当前源码默认使用 Windows Raw Socket 和随包提供的 WinDivert 接收组件；历史 `npcap_*` 模块名仍用于共享解码代码，不代表需要安装 Npcap。

## 数据与验证边界

- 队友数据的实时性取决于游戏下发的数据。团灭或中间首领的完整统计可能在下一次开怪时补齐；最后一场没有后续开怪时可能无法补全。
- 实时团队秒伤在队友累计数据尚未到达时可能采用观察到的首领血量变化作为临时汇总；它与最近一场结算秒伤、逐人技能记录是不同数据。
- 缺少的技能或治疗不表示真实数值为零。不要把部分观测当作完整战报。
- 本次回归、打包启动和旧版更新兼容验证分别记录在发布验收中。

完整记录见 [v0.4.2 发布验收](docs/release-v0.4.2.md)。

## 开发与构建

主分支包含当前版本源码。Windows 编译使用 Python 3.10 和 Nuitka，构建入口为 `build_exe.ps1`。公开分发应使用受保护构建，并在授权服务登记对应构建 ID。

```powershell
.\build_exe.ps1 -OutputDirectory dist -ProtectedRelease `
  -RuntimeProfileId "<profile-id>" `
  -CapabilitySigningKeyId "<key-id>" `
  -CapabilityPublicKey "<base64-public-key>"
```

依赖准备、开发配置与验收步骤见 [构建说明](docs/build.md)。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `dps_meter.pyw`、`main_hud*.py`、`hud_settings.py` | 桌面入口、HUD 与 HUD 设置 |
| `windows_*receiver.py`、`npcap_*` | Windows 接收与共享协议解码 |
| `encounter_*`、`settlement_*` | 战斗生命周期和结算 |
| `assets/`、`third_party/` | 界面资源、内置接收组件和许可证 |
| `docs/` | 使用、构建、验收和必要的技术记录 |
| `server/`、`daodao-card-bot/`、`web/` | 配套服务端、机器人和网站 |
| `test_*.py` | 回归检查 |

运行日志、战斗数据库、卡号、开发运行配置、密钥和构建缓存不随公开发布上传。

## 反馈

可通过程序内「反馈」提交复现步骤、版本号和可选诊断摘要，也可在 [GitHub Issues](https://github.com/cblan23/gmzzdps/issues) 报告问题。请勿在公开问题中附卡号、密钥或完整私人战斗数据。
