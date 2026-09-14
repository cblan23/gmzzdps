# QQ 机器人详细诊断

管理页面 → QQ 机器人 → 近期运行记录下方：

- 查看详细诊断：显示最近150条结构化事件。
- 导出诊断日志：下载最近最多3000条事件JSON，需要已有管理员认证。

`trace` 关联同一条命令的发卡、发送和结束事件；`message_id` 关联独立入口监听和业务接收。时间为服务器Unix时间（秒，毫秒精度），页面转换成本地显示。入口 `lag_ms` 仅为到达时间减事件时间，受QQ事件秒级精度和时钟偏差影响，不能单独解释为网络耗时。

关键事件：

| 事件 | 含义 |
| --- | --- |
| mention_received | 独立监听收到白名单群 @消息 |
| command_received | 业务识别命令 |
| command_rejected | 群白名单、管理员权限、FAQ限频、队列上限拒绝 |
| card_http / card_retry / card_failure | 后台请求状态、耗时、重试和错误类别 |
| card_allocated | 首次分配或当天原卡，不记录卡号 |
| qq_send_api | QQ发送接口返回码或超时，包含耗时 |
| private_delivery / group_reply | 私聊或群发送确认；不是对方已读证明 |
| dispatch_finished | 本次业务事件处理结束及耗时，包含忽略的消息事件 |
| heartbeat / qq_probe | 区分心跳在线与实时接口能否响应 |
| login_state / recovery_action / recovery_state | 登录变化与恢复操作 |
| container_snapshot | 实时探测失败或重启前记录运行、OOM、退出码 |

服务器文件：`/opt/daodao-card-bot/logs/diagnostic-{worker,maintenance,ingress}.jsonl`。每条链路单独写入，单文件2MiB、4个备份，共约30MiB；旧有按日轮转日志仍保留。导出有读取上限，不是无限期完整归档。日志故障不应中断发卡。

只记录允许的字段，不写聊天正文、完整卡号、Token、二维码URL、环境变量、完整异常文本。导出仍含QQ号和群号，请仅用于管理员排查，不公开转发。

部署备份：`/opt/daodao-card-bot/backups/diagnostics-20260912-215936`。仅重启业务worker、维护和独立监听，QQ容器未重启。64项测试通过；JavaScript语法与模拟DOM通过；本地和公网认证诊断API均返回三类日志，公网JS包含导出入口。未主动发送测试聊天。

这次是可观测性完善，不代表已找到反复假在线/QQ登录失效的根因。
