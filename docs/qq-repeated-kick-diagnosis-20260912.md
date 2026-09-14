# QQ 重复掉线诊断（16:10）

仅检查，未重启机器人或更换版本。

已记录的明确事件：12:27:38、15:25:22收到 `[KickedOffLine] [下线通知] 你的账号当前登录已失效，请重新登录。`。源代码由QQNT的onKickedOffLine回调转发tipsTitle/tipsDesc，并把online设false；不是发卡API错误合成的掉线提示。

14:27:44则有管理员维护action=restart_napcat，随后容器重启，再于14:29:31上线。此事件不能算未经操作的自然掉线。更早10:28/11:27缺少完整退出原因，不能追溯断言其原因相同。

当前旧机NapCat4.18.19、LinuxQQ3.2.30-50969；QQ在线，16:09:47仍成功发卡。容器OOMKilled=false、ExitCode=0；worker自15:33:25运行。新ECS Docker/socket/worker均inactive且disabled，没有发现双机同时运行。

上游相关现象：https://github.com/NapNeko/NapCatQQ/issues/2027 报告相同NapCat版本等待一段时间被踢后无法重登，但其系统/QQ版本不同；https://github.com/NapNeko/NapCatQQ/issues/1962 报告掉线后刷新接口成功但旧二维码不变。这些是相似案例，不证明当前账号具体是风控或某个版本缺陷。

结论：最近两次明确由QQ侧撤销登录会话，服务未崩溃。究竟是登录设备冲突、QQ安全策略还是客户端/协议兼容，需要进一步证据；不能仅凭通用KickedOffLine文字判定被顶号或安全风控。网页快速恢复只是恢复流程，不消除上游踢线原因。

下一步应由用户核对该QQ是否同时在其他桌面端/服务器登录，再做有备份的NapCat与QQ版本组合验证。不以自动频繁重启或保存QQ密码规避登录要求。
