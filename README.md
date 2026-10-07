# 叨叨诡秘助手

Windows 游戏战斗统计工具，提供悬浮 HUD、队伍 DPS / HPS / 承伤、首领信息、战斗记录与 PvP 场次分析。

**当前版本：v0.3.6** · 客户端构建 `0.3.6+20261007.2` · GitHub 程序包为预发布版本。

[下载 Windows 便携包](https://github.com/cblan23/gmzzdps/releases/tag/v0.3.6) · [使用指南](docs/usage.md) · [常见问题](使用问题大全.md) · [构建与发布](docs/build.md) · [验证状态](docs/release-v0.3.6.md)

## 快速开始

1. 在 [Releases](https://github.com/cblan23/gmzzdps/releases) 下载 `Daodao-GMZZ-v0.3.6-win-x64.zip`，解压到可写的文件夹。
2. 启动游戏，进入角色可操作界面；建议在进入副本前启动助手。
3. 双击 `叨叨诡秘助手-v0.3.6.exe`，允许管理员权限，在登录页输入有效卡号。
4. 连接游戏后开始战斗。底栏齿轮打开设置，`Home` 默认显示 / 隐藏主窗口。

便携包自带运行依赖，**不需要安装 Python 或 Npcap**。GitHub 自动生成的 `Source code` 压缩包是源码，不能直接替代 Windows 程序包。

## v0.3.6 更新

- 自动上传已捕获的全员战斗详情、起手与命中时间轴、装备快照、团队 DPS 和 Boss 血量曲线。
- 失败战斗保留在角色记录中，但不进入排行和职业统计；迟到的明细可继续补传。
- PVE、PVP 装备改为逐人查询，并修复评分变化后装备不显示的问题。
- 修正大帝本多阶段血量曲线、血量预测和狂暴倒计时。
- 设置新增战斗记录上传开关和 HUD 页面切换快捷键。
- HUD 使用「团队构成」「实时战斗/战斗记录」与「DPS数据」标签，并提供网页数据库入口。
- 修复小怪战斗进入未知 Boss 后仍停留在小怪段、Boss 血条不显示的问题。
- 重做网页排行和职业统计筛选。

详见 [更新日志](release-notes-v0.3.6.txt)。

## 功能

| 功能 | 内容 |
| --- | --- |
| 主窗口 | 透明悬浮 HUD、职业与玩家信息、统计数值、总量、死亡次数、战斗时间、锁定和置顶 |
| 显示设置 | 观众 HPS / DPS、战士 DPS / DT、本人高亮、队伍评分预览、字体、缩放与玩家行遮罩 |
| 首领信息 | 血量、首领识别、已有资料支持的狂暴节奏预测 |
| 战斗记录 | 本地历史、DPS / HPS / DT、技能、装备、目标和治疗分析 |
| PvP | 模式切换、竞技战绩、交手明细、猎龙之城场次与战盟查询；采集完整度仍需实战验证 |
| 登录与更新 | 卡号登录、本机加密记忆、后台检查更新、下载大小与 SHA-256 校验 |

当前源码默认使用 Windows Raw Socket 和随包提供的 WinDivert 接收组件；历史 `npcap_*` 模块名仍用于共享解码代码，不代表需要安装 Npcap。

## 数据与验证边界

- 队友数据的实时性取决于游戏下发的数据。团灭或中间首领的完整统计可能在下一次开怪时补齐；最后一场没有后续开怪时可能无法补全。
- 实时团队秒伤在队友累计数据尚未到达时可能采用观察到的首领血量变化作为临时汇总；它与最近一场结算秒伤、逐人技能记录是不同数据。
- 缺少的技能、治疗或 PvP 承伤不表示真实数值为零。不要把部分观测当作完整战报。
- 当前修复源码全量回归：**2348 项，0 失败、0 错误、3 项跳过**。新程序包的实战和干净电脑验收仍需执行，因此 Release 保留预发布标记。
- [深色设置完整设计稿](docs/settings-redesign-20261003/index.html) 已补齐显示、外观和快捷键三页；正式程序仍使用现有设置界面。

完整记录见 [v0.3.6 发布验收](docs/release-v0.3.6.md)。

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
| `dps_meter.pyw`、`main_hud*.py` | 桌面入口、HUD 与现有设置界面 |
| `windows_*receiver.py`、`npcap_*` | Windows 接收与共享协议解码 |
| `encounter_*`、`settlement_*`、`pvp_*` | 战斗生命周期、结算和 PvP |
| `assets/`、`third_party/` | 界面资源、内置接收组件和许可证 |
| `docs/` | 使用、构建、验收和必要的技术记录 |
| `server/`、`daodao-card-bot/`、`web/` | 配套服务端、机器人和网站 |
| `test_*.py` | 回归检查 |

运行日志、战斗数据库、卡号、开发运行配置、密钥和构建缓存不随公开发布上传。

## 反馈

可通过程序内「反馈」提交复现步骤、版本号和可选诊断摘要，也可在 [GitHub Issues](https://github.com/cblan23/gmzzdps/issues) 报告问题。请勿在公开问题中附卡号、密钥或完整私人战斗数据。
