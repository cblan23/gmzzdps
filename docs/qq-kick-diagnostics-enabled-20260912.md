# QQ 踢线原因日志已启用

2026-09-12 18:04在旧机器人服务器47.116.62.116加载诊断补丁，保留私聊路由保护，无更换QQ/NapCat版本。一次正常容器重启后18:05观察到QQ自动恢复在线，未要求重新扫码。

独立文件：`/opt/daodao-card-bot/data/napcat/daodao-kick-events.jsonl`。第一次实际掉线回调发生后才创建。超过1MiB轮换到`.1`，只保留当前及上一个文件。文件随已挂载配置目录持久化，不因容器重启删除。

白名单字段：UTC时间、QQ_KickedOffLine事件、kickedType、securityKickedType、sameDevice、appId、instanceId；数字仅接受安全整数，不对缺失字段填0。无聊天内容、卡号、密码、令牌或完整回调对象。原提示文字追加QQReasonFields后交给既有维护观察记录。独立文件即使管理会话失效仍可留证。

源码形状检查及2项补丁测试通过；QuickJS执行实际新增回调片段验证提取字段且忽略模拟secret。尚未主动制造真实被踢事件，原因码含义仍需依据后续实测与协议证据核对，不承诺有日志即可唯一定位腾讯内部策略。

部署哈希：de1898a742f012bbaf17fd10cc1487897e437471cb20261bde8ab4facc4118af。
旧文件备份：`/opt/daodao-card-bot/backups/kick-diagnostic-20260912-180423/napcat.mjs`。
生成器：`daodao-card-bot/scripts/prepare_kick_diagnostics.py`。升级或重新运行旧私聊补丁生成器会覆盖诊断补丁，必须重新检查并应用此诊断步骤，再核验两个保护同时存在。

这项改动是调查手段，不是QQ踢线修复；不能再以WebUI会话期限解释QQ登录会话被撤销。
