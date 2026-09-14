# QQ 双实例部署进展

09-13 08:50更新：本地已使用用户安装且签名有效的QQ9.9.35.52892配套运行文件启动；只复制至机器人专用目录，不修改D:/Tencent/QQNT。缺失DLL已解决，6109回环WebUI可访问，登录二维码已在浏览器打开，等待2864967431扫码。共享API再次验证成功；远程维护本地通道尚未完成。

按用户指定：ECS47.114.37.117使用3035610294，本地D:/vscode/daodao-card-bot-local使用2864967431。

## ECS

从旧服传输当前实际部署app，不混入本地尚未部署的自动恢复重构。保留现有权威DB/API，CARD_MODE=remote，CARD_API_URL=https://daodaogame.vip/api/card/claim；管理员和群沿用。新生成独立WebUI/WS令牌。开启已有Docker，仅有NapCat容器；重建容器并启用daodao-card-bot.service。gmzz-dps-monitor、daodao-card-api均仍active，未另起API/数据库。

备份：/opt/daodao-card-bot/backups/ecs-bot-20260913-082808。已有私聊失败关闭保护hash509fb79635581a83fea15142012cd29d795d5130d3b5e0398d5efee6ac2f4e81。

WebUI位于ECS loopback6099，本机SSH隧道6101；浏览器已打开供扫码。未确认真实登录/群消息/领卡。旧服3921054980保留，公网现有QQ管理仍路由旧服；未擅自切换。

## 本地

独立目录，ACL仅当前Windows SID和SYSTEM完全访问；独立venv、最小.env（不含后台管理密码）、独立WS/WebUI令牌、独立QQ数据。统一API只读stats验证成功。NcatBot4.4.1.post1依赖已安装。

官方Windows Node v4.18.19下载及SHA256验证成功；私聊路由保护通过执行测试。Node分叉使用Electron参数--no-sandbox导致退出码9，设置上游已有单进程开关后发现缺失crypto.dll、ssl.dll。对应腾讯50969安装包链接已失效。未混用不明DLL；本地已停止失败的worker/Node启动重试，尚不可扫码、无自动启动任务。

08:38更新：ECS已扫码上线，get_status在线，get_login_info匹配3035610294；worker在08:38:32记录napcat_connected。真实群消息/发卡仍未验收。

下一步：取得可信且匹配的完整QQ运行时/官方修复包，再启动本地，扫码2864967431并验证三群身份和跨机器人同日返回原卡。不公开真实卡号或Token，不主动群发测试消息。
