# 上海旧机 → 杭州新 ECS 迁移状态

用户授权整站迁移、尽量无感；最终仍需控制台安全组及 DNS 配合，不承诺 QQ 免扫码或所有旧客户端零中断。

- 旧机 47.116.62.116 / cn-shanghai，当前唯一生产写入源。
- 新机 47.114.37.117 / cn-hangzhou，i-bp1bay7fu67cv22b1itx，用户已用轻量共享镜像更换系统并启动。
- 新机控制台已配置出方向 IPv4 TCP/UDP 全端口拒绝；入方向截图仅确认 SSH22、RDP3389、ICMP允许。
- 用户提供新公网带宽3Mbps，更新并发吞吐仍受该配置影响，不宣称迁移即可恢复原下载速度。

## 已完成

1. 初次新机部署前安装过 nginx/python/rsync/docker，但后续被用户共享镜像整盘替换，当前以镜像版本为准。
2. 原来的初始/分阶段文件搬运已停止，未切域名。
3. 新机更换系统后SSH主机密钥改变，旧新IP条目备份到本机 known_hosts.old，已记录新公钥。现有部署用户密钥可登录root。
4. 新机13:41启动后立即正常停止 daodao-card-bot、card-api、qq-maintenance、gmzz-dps-monitor、feedback、nginx及NapCat容器；disable Docker/socket及上述服务。另disable daodao-napcat-bootstrap 和 certbot timer，防止克隆自动重启。无公网IPv6。
5. 比对签名私钥/HMAC身份密钥/.env/白名单/runtime-profile/服务代码/机器人代码的SHA256，均与生产一致。未输出原始凭证。
6. 新镜像库6339卡、15067会话、99条领卡；同期旧库6411卡、15139会话、150条领卡。说明必须做增量，未将镜像当最新数据库。
7. 新机已单独启动 gmzz-dps-monitor、feedback、nginx用于隔离验证（未enable）；QQ和Docker保持停止。
8. 新机配置所有6个Nginx server块临时拒绝非GET/HEAD/OPTIONS，标记 `DAODAO_MIGRATION_STAGING_READ_ONLY`。备份 `/srv/daodao-migration/nginx-before-guard.json`。即使开放443，也不接受生产写入。恢复时必须先最终数据同步并停止旧生产写源。
9. 新机本机验证通过：health、带鉴权admin、TLS SNI与证书、QQ维护脚本入口标记、现有d正式包哈希；旧服务仍运行，域名未变。

## 数据预同步

- 旧机 `/var/backups/daodao-migration-20260912/sessions.initial.sqlite3` 为最初一致性备份，quick_check通过，51,609,600字节，SHA b068fb4e2fd077b51d7c0f05c38f4ced49aa0aafcc1443fe5a9237056ea4a048。
- 当前后台任务 `daodao-migration-warm.service` 在旧机运行 `/tmp/migration_warm_sync.py`，生成 sessions.warm.sqlite3 后，以rsync压缩/校验/差异传输到新机 `/srv/daodao-migration/`；未覆盖新机验证数据库。
- 旧机 warm-manifest.json 保存快照hash；warm-sync.log保存rsync统计。完成后需核对新机快照hash/quick_check。
- 迁移临时密钥只在旧机 `/root/.ssh/daodao-migration-20260912`，新机授权限制来源47.116.62.116且2026-09-14到期。迁移结束须删除新机对应公钥授权及旧机临时密钥；不删除用户原密钥。
- 旧机到杭州新机的SSH直传也偏慢；不能承诺大文件同步几秒完成。

## 下一步

1. 用户在新机安全组入方向允许TCP80/443；确认克隆服务已停用后可移除仅为迁移新增的两条出方向拒绝规则，DNS仍不改。
2. 实测新机公网TLS/静态网站/下载Range，验证仍有写保护；核对全部站点静态文件及证书路径。旧站点保留可回退。
3. 准备旧机转发新机、Host/TLS/真实来源IP处理。确认旧机到新机443可达，避免转发循环。域名DNS尚无云API权限。
4. 切换时冻结旧写服务和旧机器人，最后一致性备份/增量与QQ Session离线同步；新机监控停服后安装最终DB（保留原验证库备份，不覆盖已产生的生产数据）。核对卡号、领取、会话、上传表及密钥。
5. 启用新机生产、取消写保护，旧机转发到新机，旧机不再写DB；只启动新机机器人。最后用户切DNS，观察后再讨论旧机释放。

当前迁移未完成、未切流量、旧服务器未停用。在线更新暂停状态与其他未完成客户端修复保持原状，不能在迁移中混入未发布版本。

## 14:20 最新状态（优先于上文计划）

用户改为拆分架构并授权实施：新 ECS 为唯一数据库/鉴权/发卡 API/网页服务器；旧机保留QQ容器、机器人及维护执行服务；更新包计划OSS/CDN。旧机不再承担数据库写入。

- 第一轮普通rsync最终同步在35秒预算内未完成，约14:09自动恢复旧机写服务；QQ从未重启。未在该轮向新机开放写入。
- 改用共同备份作为基准生成二进制差异：50MB数据库 warm差异包464225字节，新机还原SHA256匹配并quick_check通过。
- 第二轮14:13:38～14:13:49完成最终切换，程序记录暂停10.533秒；6434卡、15187会话、132反馈、14战斗/上传、164领取、178请求记录完整迁移。最终差异与表计数验证通过。
- 新机 `/srv/daodao-migration/production-open.json` 标识生产写入开放；旧机 `/var/backups/daodao-migration-20260912/split-cutover-result.json` 标识切换完成。
- 切换后因Nginx上游默认证书验证深度不足出现502，约14:16修正为proxy_ssl_verify_depth 5（保留证书验证）。因此不能宣称本次完全无感；部分客户端发生重连，3次机器人领取请求失败后可由用户重试。不得隐瞒该窗口。
- 修复后公网health HTTP200约0.135秒；14:19:57新机1122次写事务、0排队超时、最大排队7.09ms、最大持锁231ms，43个登录会话。已有23个切换前会话续期，另有新会话建立。
- QQ在线、旧worker连接正常；14:16:23实收用户重复领卡请求已通过新API发回原卡。未为了验收主动发QQ消息。
- 旧Nginx `/etc/nginx/conf.d/daodao-cutover-forward.conf` 将业务转发新IP，特殊 `/api/v1/dps/admin/qq/` 和维护JS仍代理旧环回8771。新机这两个维护路径通过TLS代理旧IP，双向已验证无循环。
- 新机信任旧IP提供的X-Real-IP，后端保留真实客户端IP。新机bot/API配置CARD_API_URL为loopback8770；旧机CARD_API_MANAGED_LOCALLY=0，API故障不会重启已退役的本地卡号服务。
- 新机启用gmzz-dps-monitor、feedback、card-api、nginx开机启动；新机Docker和机器人/维护执行服务disabled。旧机gmzz/feedback/card-api disabled，QQ/Docker/worker/maintenance继续enabled。
- 两边certbot renewal timer暂禁用，DNS切换确认后需启用新机续期；旧转发服务器证书到期前必须制定续期或下线策略，不能长期忽略旧IP客户端。
- 临时跨服务器迁移公钥授权及旧机临时密钥对已删除，用户部署密钥保留。旧跨主机脚本不可直接重跑，尤其不得重启旧数据库写源。

### 仍需完成

1. DNS三条A记录：`daodaogame.vip`、`www.daodaogame.vip`、`gmzz.daodaogame.vip`目前仍解析47.116.62.116，需用户改47.114.37.117。否则主程序请求仍经过旧机的公网入口，尚未完全隔离旧出口故障。
2. OSS/CDN尚未开通或授权。两机无Aliyun/OSS CLI配置，新机RAM role元数据404；不能用SSH替代云控制台权限。不得宣称下载已分流或恢复。
3. 在线更新下载仍503暂停，原d发布元数据与白名单保留。r2包只在本地，未混入本次迁移。
4. 观察峰值流量和机器人登录持续性；几分钟健康检查不能证明长期稳定。

## DNS与续期收尾（14:27～14:35）

- 用户已修改3条A记录。直接查询权威dns23.hichina.com，主域名/www/gmzz均为47.114.37.117，TTL600。部分递归/系统缓存仍短暂返回旧IP，兼容代理保持可用。
- 新机新旧域名路径与机器人维护均验证通过；14:27时52个在线会话、3644次写事务、0排队超时；14:30/14:31继续有实际QQ发卡成功。
- 新机已将旧IP的Certbot续期配置移到 `/srv/daodao-migration/old-ip-renewal.conf` 留存，启用 `snap.certbot.renew.timer` 维护主域名及gmzz证书。
- 旧机增加 `daodao-gateway-cert-renew.timer/service`，仅续期主域名（维护上游TLS使用）和旧IP（硬编码旧IP客户端使用）证书。旧机默认certbot timer继续禁用；gmzz的旧机证书不再自动续期，域名现在由新ECS续期。
- 新机HTTP主域名的ACME挑战本地文件优先，缺失时只将公开challenge请求转发旧机HTTP。旧机HTTP对应路径强制本地读取，避免循环。新旧两条路径均用临时公开测试token验证200，测试文件已删除。没有导出/传输新的私钥，没有关闭TLS校验。
- 这是续期路径与定时任务检查，不等同于实际向CA执行了一次签发；后续仍需监控定时任务结果。
- OSS/CDN仍未开通/授权，在线更新下载仍暂停。下一步需要用户打开OSS控制台确认Bucket及授权方式；不能凭SSH创建云侧资源。
