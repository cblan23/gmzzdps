# Daodao Card Bot / 叨叨同行卡助手

## 当前正式规则（用户后续确认，优先于下方导入卡池说明）

- 群领取优先支持临时私聊：调用NapCat `send_private_msg` 时传真实发送者user_id、来源group_id并固定message_type=private。好友仍走普通私聊，非好友走群临时会话；QQ限制或群禁用临时会话时才提示检查设置/加好友。
- 部署的NapCat版本有“UID解析失败且存在group_id时退回群聊”的分支。本项目使用 `scripts.patch_napcat_private_route` 将明确private请求改为失败关闭，禁止该回退。原文件保存于data/runtime，修改后的文件只读挂载；升级NapCat必须重新核对和验证此保护，不可移除保护后继续发送含group_id的卡号消息。
- 首次启动Compose前先执行 `.venv/bin/python -m scripts.patch_napcat_private_route`；脚本从已固定镜像临时容器提取源码，验证已知函数结构后生成挂载文件，不需要先启动QQ。可以安装requirements-dev.txt并运行 `python tests/verify_napcat_private_guard.py data/runtime/napcat.mjs` 验证陌生人、好友、未知身份三种路由。

- 机器人QQ：3035610294；管理员QQ：1806525。
- 允许群：165966739、1094925831、732363944。
- 正式 `CARD_ISSUANCE=generated`：第一次领取调用现有DPS后台新生成真实卡，卡种 `bot_8h`，名称“机器人发卡专用”，首次登录激活后8小时。2026-09-12切换之前生成的12小时卡保留原时长，重复领取返回卡记录本身的时长。
- 无需手工补充卡池；同QQ当天跨群仍只生成一次，私聊失败/网络重试返回原卡。
- 生成卡与领取记录在现有DPS SQLite库的同一写事务内提交，新增 `bot_card_claims` / `bot_card_requests`；卡号采用现有生成函数与鉴权规则。
- 发卡API独立服务转发到本机DPS `/api/v1/dps/bot/claim`，相同 `.env` 中服务Token认证；后台再次检查群白名单。管理状态显示“剩余：按需生成”。
- `CARD_ISSUANCE=pool` 才使用下方的预导入专用卡池。`CARD_MODE=local` 是机器人独立开发测试，生产不要切到local。
- 未激活卡暂无确定到期时间，私聊显示“机器人发卡专用 请注意登录时需要点击登录而不是试用”；已激活再查询时显示实际契约截止时间，不按领取时间虚构期限。
- 部署生成接口需要把项目外的 `server/bot_cards.py` 和更新后的 `server/dps_monitor_server.py` 一并部署。`scripts/deploy_generation_backend.py` 为本次已审核版本的定向部署脚本，包含旧版本哈希检查，后续版本请重新审核，不绕过校验。

修改机器人/群配置后，除了重启Bot/API，还需要重启DPS后台使其读取新的白名单（由systemd引用同一个.env）。不要从公网传入任何用户自报身份来调用该受信服务接口。

机器人显示名：**叨叨助手**（在手机 QQ 修改机器人账号昵称/群名片）。Python 3.10+，推荐 Ubuntu 24.04 / Python 3.12。

## 架构与完成边界

NapCat Docker → 本机认证 WebSocket → NcatBot connect-only → BotHandler → CardService → 发卡 API → 独立 SQLite 卡池。
生产默认 RemoteCardApiService，发卡权威在 API，机器人不决定每日分配。LocalCardService 仅用于开发。
NcatBot 固定 4.4.1.post1，已按该安装包实际 API 适配 `napcat.remote_mode=True`、`enable_webui_interaction=False`、`BotClient.on_group_message` 和 `send_private_msg`。不让 NcatBot 安装 NapCat。

本仓库提供可运行实现、API、并发和真实 NcatBot/模拟 OneBot 集成测试。真实 QQ 在线、好友私聊权限、完整重启免扫码必须在用户扫码后验收；QQ 风控或 Session 失效仍可能要求再次扫码，不保证永久免扫码。

不记录 QQ 登录密码。账号只配置 BOT_QQ。所有应用 Token 源头为服务器 `.env`，不提交 Git、不在日志输出。NapCat 必须读取的认证设置由脚本从 `.env` 渲染到私有持久目录，属于运行时配置，纳入敏感备份。

## 领取规则

- 必须是真实 `at` 消息段指向 BOT_QQ，文字精确为“领卡”；单纯文本伪装 @ 或“领卡 123456”不接受。
- `event.user_id` 和 `event.group_id` 为身份来源，拒绝匿名消息、机器人自己消息及其他账号事件。
- Bot 和 API 双重群白名单；白名单为空时拒绝发卡。
- 以 UTC+8 北京时间跨日，一个 QQ 在所有允许群之间共用每天一张规则。
- SQLite `BEGIN IMMEDIATE` 原子领取；`UNIQUE(qq,claim_date)`、`UNIQUE(card_code)`、`UNIQUE(card_id)` 防止重复分配。
- 消息 request_id 保证跨午夜网络重试仍返回原卡；新消息到第二天可以领第二天卡。
- API 返回后私聊失败，卡仍属于原 QQ 当天，再次发送原卡，不释放，不自动换卡。
- 群回复由固定模板构造，不拼接 API 返回的卡号或原始错误。
- 卡池空返回业务错误，不退出。发卡 API 超时最多重试两次且使用相同 request_id。
- 发卡 API Bearer token 是机器人服务凭据，不能发给群成员或放前端。其他入口接入时须有自己的可信身份认证适配，不能将公开用户输入 QQ 当身份。

## 凭证

| 配置 | 用途 |
| --- | --- |
| BOT_QQ | 机器人账号号码，不是密码 |
| ADMIN_QQS | 管理员 QQ，逗号分隔 |
| ALLOWED_GROUPS | 可领卡群号，逗号分隔 |
| NAPCAT_WEBUI_TOKEN | NapCat 管理页面认证，不是 QQ 密码 |
| NAPCAT_TOKEN | NcatBot → OneBot WebSocket 认证 |
| CARD_API_TOKEN | Bot → 发卡 API 认证 |

三个 Token 必须不同，建议各36字节以上随机值。安装脚本自动生成，不打印。改配置请编辑 `/opt/daodao-card-bot/.env`，不要把文件贴到聊天或工单。

## Ubuntu 从零安装

先检查剩余资源。QQ/NapCat 与现有服务共享小服务器时需要关注内存；Compose 给 NapCat 768MiB、0.8CPU 上限，Bot/API 分别256/192MiB。若发生容器 OOM，检查宿主可用内存再调整，不要取消所有限制。

将项目放在 `/opt/daodao-card-bot`：

```sh
cd /opt/daodao-card-bot
sudo sh scripts/install_ubuntu.sh
sudoedit /opt/daodao-card-bot/.env
```

安装脚本通过 Ubuntu 官方软件仓库安装 `docker.io docker-compose-v2 python3-venv ca-certificates`，启用 Docker，创建专用低权限系统用户、Python venv 和两个 systemd 服务，不安装或调用旧 DPS 采集器。安装系统依赖可能更新 Python 补丁版本，按终端提示安排其他进程的维护，不应重启游戏或用户客户端。

填写 BOT_QQ、ADMIN_QQS、ALLOWED_GROUPS。生产 CARD_MODE=remote，CARD_API_URL=`https://daodaogame.vip/api/card/claim`。也可以将同机地址设为 `http://127.0.0.1:8770/api/card/claim` 降低对公网回环的依赖；仍走独立 API/认证，业务不进入 Bot。

发布域名 API（只增加 `/api/card/claim` 与 `/api/card/stats`，不覆盖 DPS 路由）：

```sh
sudo /opt/daodao-card-bot/.venv/bin/python -m scripts.publish_nginx
```

该脚本适配当前 `/etc/nginx/conf.d/daodao-domain.conf`，备份原文件、`nginx -t` 后 reload。其他机器请根据自己的443站点添加同等反代，API只监听127.0.0.1:8770。

## 启动 NapCat、扫码

```sh
cd /opt/daodao-card-bot
sudo .venv/bin/python -m scripts.prepare_napcat
sudo chown -R 1000:1000 data/napcat data/qq
sudo docker compose pull napcat
sudo docker compose up -d napcat
sudo systemctl restart daodao-card-api daodao-card-bot
```

首次可用镜像拉取后应固定 digest（参见升级节），避免 `latest` 意外变更。

在自己的电脑开 SSH 隧道：

```powershell
ssh -N -L 6099:127.0.0.1:6099 root@47.116.62.116
```

使用密钥时加 `-i 私钥路径`。浏览器打开 `http://127.0.0.1:6099/webui`，输入 `.env` 内 NAPCAT_WEBUI_TOKEN，然后用手机 QQ 扫码并确认机器人账号。不要把 Token 放浏览器网址或截图发出去。
OneBot配置应为：正向WebSocket、0.0.0.0:3001、Token与NAPCAT_TOKEN一致、array消息格式、心跳30000ms。预置 `onebot11.json` 和（配置QQ后）`onebot11_<QQ>.json`；以WebUI实际启用状态为准。

宿主6099和3001均只绑定127.0.0.1。安全组仅保留22/80/443，22按管理员出口IP收紧。不要打开6099、3001、8770，也不要挂载Docker socket进QQ容器。

## Session 持久化与确认

`data/qq → /app/.config/QQ`，`data/napcat → /app/napcat/config`。不要运行 `docker compose down -v` 或清空这些目录。

```sh
sudo .venv/bin/python -m scripts.check_login
sudo docker compose restart napcat
# 等待服务恢复，再检查：
sudo .venv/bin/python -m scripts.check_login
sudo docker inspect --format '{{json .Mounts}}' daodao-card-bot-napcat-1
```

检查输出 connected/logged_in/correct_account 均为true；重启后仍为true才算本次Session恢复验收成功。先不要拿整个ECS重启当首次测试，以免影响在线DPS用户。扫码窗口关掉、SSH隧道断开不影响后台运行。

## 卡池初始化与补充

API 使用 `data/cards.db`，与DPS鉴权库分开，不自动占用现有销售卡。
正式卡池必须导入**已在DPS后台创建、专供群领取且不会另行销售的真实卡号**；导入不会生成可用的DPS授权，也不会自动设置有效期。首次需要运营方确定要发的卡种和时长。不要把现有销售库存全量导入。

```sh
cd /opt/daodao-card-bot
sudo -u daodao-card-bot .venv/bin/python -m app.manage init
sudo -u daodao-card-bot .venv/bin/python -m app.manage import --file /安全路径/专用卡池.txt
sudo -u daodao-card-bot .venv/bin/python -m app.manage stats
```

每行一张，重复导入忽略已有卡，不重置已领取卡。文件只作为导入入口，正式分配始终由API数据库事务完成。勿将卡文件提交Git；导入源应限制读取权限。
API当前没有权威有效期数据时返回null，机器人不会凭空显示“契约至”。以后对接现有卡元数据时填expire_at即可。

## 开发测试模式

在独立目录/独立 `.env` 设置 `CARD_MODE=local`、`CARD_DB=data/test-cards.db`，导入：

```text
DAODAO-AAAA-BBBB
DAODAO-CCCC-DDDD
DAODAO-EEEE-FFFF
```

这是测试卡，不可用于DPS登录。不要导入生产API库。

```sh
python -m unittest discover -s tests -v
python tests/verify_ncatbot_wire.py
```

覆盖并发同QQ、20个不同QQ、跨午夜重试、重复导入、卡池空、API鉴权、群白名单、伪造文本@、管理员权限、私聊失败保留原卡。
第二条使用真实安装的NcatBot和本机模拟OneBot，不发送真实QQ群消息，验证框架适配与日志不泄露。

## 真实群验收

1. 手机QQ确认机器人在线、昵称/群名片为叨叨助手。
2. 管理员把机器人拉入允许群；必要时先加好友。机器人不自动批准陌生入群或好友请求，请通过QQ客户端人工处理。
3. 群成员发真实 `@叨叨助手 领卡`。确认只在本人私聊收到卡，群里仅收到成功提示。
4. 同QQ再次发送，核对卡相同、库存只减少1。
5. 两名成员同时领卡，核对卡不同。
6. 未加好友/禁临时会话时，确认失败提示；加好友后再领原卡。
7. 非允许群无领取；普通用户“卡池”无管理权限。
8. 管理员“卡池/状态”显示累计库存与当天领取人数。“查询/重置/补卡”保留入口但明确回复未开放；不会执行危险重置。补卡目前通过上面的服务器CLI。

“总数/已领取/剩余”是专用池累计库存，另列“今日领取”，避免跨日歧义。系统不自动让昨天领过的卡重新入池。

## API 契约

`POST /api/card/claim`，Bearer CARD_API_TOKEN，JSON `{qq,group_id,request_id}`。request_id可省略，但机器人始终提供真实消息生成的稳定键。
成功：`{success:true,already_claimed:false|true,card,expire_at:null|string,claim_date}`。
空池：`{success:false,code:"CARD_POOL_EMPTY"}`。
401认证失败、403群不允许/管理权限不足、400格式错误、503数据库繁忙、500内部错误。
`POST /api/card/stats` 需要同一服务Token和受信管理员 `admin_qq`。不要给浏览器/群成员直接调用凭证。

## 运维

```sh
sudo sh scripts/logs.sh
sudo systemctl status daodao-card-api daodao-card-bot --no-pager
sudo docker compose ps
sudo sh scripts/restart.sh
sudo sh scripts/stop.sh
sudo sh scripts/start.sh
```

NapCat Docker禁止输出普通日志，因为上游可能输出WebUI Token或完整消息；排查登录用WebUI、check_login以及容器状态。NcatBot第三方日志被丢弃，业务日志仅记录QQ/群号/操作/错误码，不记卡号、Token、原始消息或异常正文。日志北京时间以系统TZ为准，按天轮转保留14份。

NcatBot断连时独立监督进程按退避重启worker，最长约60秒；systemd负责监督进程的恢复。等待用户配置时每10秒检查一次。修改.env后应 `systemctl restart` 两服务，避免旧配置残留。
Docker `unless-stopped` 会在宿主重启后恢复原来运行的NapCat；人为stop的容器不会自动恢复。systemd两服务都enabled。不要同时启用systemd Bot和Compose的container-bot，避免重复回复。

### 更新应用

备份后只替换 app/scripts/requirements 等源码，保留 .env/data/logs/backups。执行 `.venv/bin/pip install -r requirements.txt`、自动测试，再 `systemctl restart daodao-card-api daodao-card-bot`。不需要重启NapCat或DPS服务。

### 更新 NapCat

先做Session备份并记录旧image digest。维护窗口执行 `docker compose pull napcat`，核实镜像来源和版本，再 `docker compose up -d napcat`。登录和私聊验收后将 `.env` 的 NAPCAT_IMAGE 改为 `mlikiowa/napcat-docker@sha256:实际摘要` 并重建，锁定镜像。
升级失败可改回旧digest，使用原volume恢复；不保证上游新旧Session格式兼容，应保留升级前备份。

### 备份

`sudo sh scripts/backup.sh`：SQLite在线backup API备份真实DPS数据库（含机器人生成卡/领取表）和可选本地池；短暂停NapCat后打包QQ/config/.env，再自动启动。备份包含全部卡数据、登录Session与Token，权限0700/0600，必须加密离机保存，不要上传群文件。恢复DPS数据库需安排维护窗口并停止DPS后台，不能在业务运行中覆盖。
恢复时停止API/Bot/NapCat，恢复数据库和QQ/config目录、修正属主，再启动。`.env` 不要用示例覆盖。

## 故障排查

| 情况 | 检查 |
| --- | --- |
| 等待配置 | BOT_QQ、ADMIN_QQS、ALLOWED_GROUPS和Token是否填写，重启Bot/API |
| WS连接失败 | NapCat已扫码？WebUI正向WS启用？Token一致？127.0.0.1:3001可达？ |
| 登录账号不一致 | 使用BOT_QQ对应账号扫码，切勿混用个人QQ |
| API超时 | 本机8770健康、nginx配置、域名证书；队列繁忙稍后重试 |
| 私聊失败 | 添加机器人好友，检查QQ临时会话/风控，稍后再次领取原卡 |
| 卡池为空 | 用专用真实卡池补充，不自动从销售库存抽取 |
| 重启需扫码 | volume路径是否正确、Session是否过期/被QQ吊销；查看check_login |
| 容器启动退出 | `docker inspect` 查看ExitCode/OOMKilled，检查磁盘和内存 |
| 镜像拉取失败 | 验证registry连接；使用官方可信镜像离线导入，不用不明镜像源 |

## 上游依据

- NcatBot安装版本与接口以PyPI分发包源码核对：https://pypi.org/project/ncatbot/4.4.1.post1/
- NapCat Docker配置与持久化路径：https://github.com/NapNeko/NapCat-Docker
- 本次发现PyPI元数据指向的旧NcatBot GitHub地址无法clone，因此使用实际wheel验证，未凭过期示例猜接口。
