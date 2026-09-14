# 当前部署交接

## 8小时卡切换（2026-09-12 09:13 北京时间，已上线）

新生成卡改为bot_8h，28800秒；管理端生成选项同步为“机器人发卡专用（8小时）”。部署前后核对15张已有12小时机器人卡，全部仍为43200秒，无数据迁移/批量改时长。
重复领取从原卡数据库行返回duration_seconds，不能用新的默认8小时覆盖旧12小时卡的返回值。
66项后台测试通过，包括旧卡当天重复领取仍12小时、新卡8小时、管理端生成8小时。
生产文件sha256：server=297c965cd2236eee14ba466030c61082af472523c2331227e478c733b04c6ab8，bot_cards=3269f451741611c5c2c4bd8a36c7350f7cfcb83f076b62edb5ecee18df302942。
备份：/opt/gmzz-dps-monitor/backup-bot8-20260912-091300。只发布卡种与管理页变更，未混入待验收的租约TTL默认值改动。

## 群临时会话修复（2026-09-12）

已部署来源群group_id的private发送适配。NapCat实际部署版本含UID失败回退群聊分支，已为explicit private请求加失败关闭保护；脚本验证好友/private、非好友/temp、未知UID/error三种情况。
11项Python测试及真实NcatBot/模拟OneBot联调通过。未向真实用户主动发送测试卡。
保护后的napcat.mjs SHA256：509fb79635581a83fea15142012cd29d795d5130d3b5e0398d5efee6ac2f4e81。
Compose先以unzip -n补齐镜像内依赖，不覆盖只读挂载的保护文件与持久化配置，避免上游entrypoint因main文件已存在而跳过其余依赖。
容器重建后WebUI正常，但QQ未恢复登录，一次官方快速登录尝试未成功；已请用户重新扫码。不能宣称免扫码恢复已通过。Bot服务等待QQ上线后自动重连。

ECS：47.116.62.116，Ubuntu，项目目录 `/opt/daodao-card-bot`。
机器人3035610294，管理员1806525，允许群165966739/1094925831/732363944。

已安装Docker/Compose、Python虚拟环境、发卡API及NcatBot监督服务，systemd与Docker已启用开机启动。
生产API地址 `https://daodaogame.vip/api/card/claim`，只接受.env中的Bearer服务Token。
按需生成的真实卡种为 `bot_12h` / 机器人发卡专用，首次登录激活后12小时，不改变已有卡种和鉴权口径。
已部署后台模块 `server/bot_cards.py`，领取记录与真实卡同事务。64项后台测试通过；机器人9项测试及Linux实际NcatBot/模拟OneBot端到端通过。
普通日志无卡号/Token。NapCat所有持久路径仅在data目录，6099/3001仅绑定本机。

剩余真实验收必须等NapCat容器启动和用户扫码：QQ登录、添加好友/临时会话、三个群实际领取、QQ Session重启恢复。
截至本次记录，官方Docker Hub直连被阻，镜像摘要缓存下载仍在进行，不能称QQ机器人已经在线。
官方Linux amd64 manifest：`sha256:406611383c31cc102665207b13cf0a4c2b463e27e300ba6ee5e7cb29adabd93f`。
官方config/image ID：`sha256:b132563daa43114c9796540fc5ed25843d62c78676e8bef7f9bfa639d1c1467c`。
缓存镜像必须匹配上述摘要。下载完成执行 `sudo sh scripts/finish_napcat_install.sh`，脚本核对image ID后才启动。

扫码入口：电脑运行 `ssh -N -L 6099:127.0.0.1:6099 root@47.116.62.116`，浏览器访问 `http://127.0.0.1:6099/webui`，使用服务器.env中的NAPCAT_WEBUI_TOKEN登录；Token不是QQ密码。
扫码后运行 `.venv/bin/python -m scripts.check_login`，确认correct_account=true，再在允许群发送真实@领卡。

源代码配套集成文件在主项目 `server/bot_cards.py` 和 `server/dps_monitor_server.py`；README给出部署、备份、恢复和常见故障处理。
