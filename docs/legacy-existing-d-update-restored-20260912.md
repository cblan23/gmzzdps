# 旧客户端下载既有 v0.2.3d：恢复结果

按用户最新要求停止新包构建，不发布0.2.3+20260912.3；保留现有已发布d成品。

2026-09-12 15:54移除新ECS域名配置中的TEMPORARY_UPDATE_DOWNLOAD_PAUSE_20260912精确location。Nginx校验/reload通过；只恢复原同源下载处理，无跨域重定向。备份 `/etc/nginx/conf.d/daodao-domain.conf.before-restore-existing-d-20260912-155404`。

保持不变：

- 版本0.2.3d、构建0.2.3+20260912.1。
- build_id b627ea4ce47245c0b73a1190ed849648。
- EXE 81,046,528字节，SHA256 b5819c0c95d8944bd2088045d49d7fdcbadae7212c9239481080d6e5134d63f0。
- update.json原字节未改，白名单未修改；既有d不含其后混合队伍累计回退修复。

用保存的旧licensing.py真实执行check_update和download_update完整流程，225.52秒下载并通过SHA256校验，未安装/启动下载文件。未向测试提供真实用户会话Token。该结果证明这一次旧版兼容更新成功，不保证多用户共享3Mbps时仍能在300秒完成。

兼容线路仍经ECS出口，约350KB/s。CDN独立URL已经完整验证（首次约11.9秒，命中缓存后更快），但没有把旧版带Authorization的请求直接302到CDN。旧Python urllib会保留此头，因此“旧版在线更新已恢复”不等于“旧版已无改动直接走CDN”。

直接下载现有d的CDN地址：
https://downloads.daodaogame.vip/releases/b627ea4ce47245c0b73a1190ed849648/Dps-Logs-v0.2.3d.exe

源码里的CDN直连、续传和混合队伍修复尚未正式发布。release-v0.2.3d-r3为已中断构建，不得分发；r2不含后续混合队伍/CDN接入修复。

## 16:07 用户要求旧版无客户端改动直接走CDN（当前状态）

用户在已说明旧urllib跨域携带Authorization的情况下继续要求CDN直链，按此要求启用服务端兼容跳转。目标仅为当前账号的固定HTTPS CDN不可变版本URL；未关闭文件校验，也未更换客户端包。

- 未命中缓存的独立测试对象带虚构Bearer时，CDN/私有OSS回源仍正常；未使用真实用户令牌测试。
- `update.json` 增加固定 `cdn_download_url` 和显式 `legacy_cdn_redirect=true`。服务端严格校验URL与当前build/版本匹配，下载请求返回302且Cache-Control:no-store。
- 原更新检查路径、显示版本、build ID、文件名、大小、哈希和白名单保持不变；原文件保留。
- 旧版完整下载流程实测81,046,528字节，命中CDN缓存后2.91秒完成，SHA256一致。这个时间是当前测试网络的结果，不保证每个用户相同。
- 程序/SQLite备份 `/opt/gmzz-dps-monitor/backup-legacy-cdn-20260912-160722`；元数据备份 `/var/lib/gmzz-dps-monitor/update.json.before-legacy-cdn-20260912-160723`。
- 当前服务端源码SHA256 dd0d1b20621241b90bb7bb22189f466b1b29a7c3e873a44558dd6b34bde9b04c。
- 旧客户端仍会把已有Authorization头带给CDN边缘；此兼容选择并未修复旧客户端行为，不能声称鉴权头已剥离。新客户端源码的独立CDN下载器会去除该头，但未构建发布。
- 后续换新版本需先上传/验证新不可变CDN对象，再更新对应元数据及显式跳转开关。没有有效CDN URL时默认回到同源完整下载，避免误跳旧文件。
