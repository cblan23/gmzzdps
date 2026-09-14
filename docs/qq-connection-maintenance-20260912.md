# 管理会话提前续期与发卡连接恢复

16:50:31已部署旧机器人服务器47.116.62.116，仅更新app/maintenance.py并重启daodao-qq-maintenance.service。QQ容器启动时间及发卡supervisor PID保持不变；领取数据、鉴权服务及发卡规则未修改。

新增行为：

- 每分钟检查一次管理凭证，50分钟本地有效期前5分钟主动换取新凭证；只在服务器内存保存。
- 提前续期失败时保留仍有效凭证，至少65秒退避，过期凭证不返回。
- QQ实际在线、worker持续45秒没有新鲜连接证据，且不在其他维护操作中时，恢复发卡连接；启动后有60秒观察窗口，自动恢复间隔至少5分钟。
- QQ离线、被踢或状态未知时，不自动重启QQ，不自动调用快速登录，不清理Session，也不保存密码；仍由管理员扫码或网页恢复。

40项机器人测试通过，涵盖提前续期、续期失败、过期凭证、自动恢复退避、在线健康不重启、QQ被踢不重启等。

线上部署后：QQ online=true、worker connected=true，management_session_refreshed事件已记录。备份 `/opt/daodao-card-bot/backups/connection-maintenance-20260912-165031/maintenance.py`。

管理会话与QQ登录是不同状态。此改动改善维护接口持续可用和发卡worker恢复，不证明能消除QQ服务端KickedOffLine。没有把一小时观察规律当成确切根因，也尚未经过跨一小时实测。
