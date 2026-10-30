# v0.4.2 发布验收

## 发布标识

- 显示版本：`0.4.2`
- 客户端构建：`0.4.2+20261030.3`
- Build ID：`0fa3ec1db7c6497eb0da6a9463498a91`
- 采集后端：`windows_raw`（WinDivert）
- GitHub Release：<https://github.com/cblan23/gmzzdps/releases/tag/v0.4.2>
- 国内 CDN：<https://downloads.daodaogame.vip/releases/0fa3ec1db7c6497eb0da6a9463498a91/Dps-Logs-v0.4.2.exe>

## 本版范围

- 增加五月庄园·城堡困难模式子嗣守护、一号信徒和子爵夫人的 8 分钟狂暴预测。
- 将一号信徒开场双 Boss 与后继安西娅合并为一场战斗；将子爵夫人与神话姿态合并为一场战斗。
- 修复双 Boss 血量消息交替时实时 DPS 行反复清空的问题；短暂不完整快照只复用本场最后一份完整数据。
- 战斗结束、结算、换战或切场景后清空实时窗口，不再回显上一只 Boss 或上一场队伍结果。
- 上传与网页公开战报统一显示“一号信徒”，网页入口和 Boss 数据页切换到当前困难副本。

## 产物

发布目录：`D:\vscode\gmzzdps\release-v0.4.2-20261030-realtime-hud-flicker-fix`

| 文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `Daodao-GMZZ-v0.4.2-win-x64.exe` | 69,046,784 | `bdd5e2bb48e75ae4c2550a21bd5bcb0ce75e64be608dd24d6ddbbf0546f4ae26` |
| `Daodao-GMZZ-v0.4.2-win-x64.zip` | 68,939,081 | `0074d73ffedb4408044b5e0aea9918c735d11e20a1366ee2785480db3d2df98b` |
| `SHA256SUMS.txt` | 196 | `7fd4087d11b0fd7daf5c076b378b483d9074dda350706b74459a11ae97fb3bc5` |

包校验器确认 EXE 为受保护构建、不含源码和测试、资源哈希匹配、采集后端为 `windows_raw`。ZIP 完整性和内嵌 EXE 哈希均已核对。

## 自动化验证

- Boss、HUD、上传与相关聚焦回归：373 项通过。
- 全量回归：2,092 项通过，2 项跳过，0 失败，0 错误。
- 旧版 `0.4.1+20261025.2` 可收到 v0.4.2。
- 当前构建 `0.4.2+20261030.3` 不重复更新；更高构建与更高版本不降级。
- `/api/v1/dps/update/download` 的 GET 返回 302，并带 `Cache-Control: no-store`。
- CDN 已在服务器发布流程和公网客户端各完整回读一次，均为 69,046,784 字节且 SHA-256 匹配。

## 生产发布

- 发布时间：2026-10-30 21:25（Asia/Shanghai）。
- 服务器备份：`/var/lib/gmzz-dps-monitor/release-backups/before-v042-0fa3ec1db7c6497eb0da6a9463498a91-20261030T132556Z`
- 发布元数据：`/var/lib/gmzz-dps-monitor/release-metadata/v0.4.2-0fa3ec1db7c6497eb0da6a9463498a91`
- 服务：`gmzz-dps-monitor.service` 保持 `active`，PID `2714715`，未重启。
- 构建白名单：保持 78 条，发布前后字节未改变；SHA-256 为 `71efe7684296a58e2eb6a7be3ecd0721361531c9bba87b5241fd73dd61f19cdd`。
- 线上 `update.json` SHA-256：`ba8a1b09cd5f3c751811dc48a9da5c414808451f078dd6481ae2e25f9c57e06e`。

## 实战与网页验收边界

- 已完成困难·子嗣守护真实结算上传：Battle ID `d71630b2f57c4ae9b2084bd6508f70a0`，服务器 encounter `enc_MmVR6RerRJF1kzdnMrMCh33r`；公网详情与角色查询均能找到该记录。
- 一号信徒的三个原始模板保留在证据字段中，但公开上传归类为一场“一号信徒”。
- 一号信徒双 Boss 消息交替时的完整防闪烁实战，以及子爵夫人两个姿态的完整实战，仍待继续验收；自动化回归和静态配置不能替代这两项实战结论。
- 本次完成公网 HTTP、更新 API、CDN 与上传查询验证；浏览器控制服务不可用，因此未声明完整视觉浏览器验收。
