# Npcap 主版本测试包

- 输出目录：`release-v0.2.3b-npcap-test`
- 文件：`叨叨诡秘-Dps-Logs-v0.2.3b.exe`
- 版本：`0.2.3b`；客户端构建：`0.2.3+20260911.5`
- Build ID：`d9ce049bfa3842eeb058535781a1fe62`
- SHA256：`ed2527a683b4851c5eb747261dce84a040358b66818a632774bbc5d0716f8631`
- EXE 大小：81,503,744 字节
- 后端：Npcap；受保护构建，延续上一正式版的鉴权公钥和非 Authenticode 发行方式。
- 数据目录：`GMZZDpsMeter`，保持正常主程序互斥锁及版本名称。
- Nuitka 完整构建通过；编译报告检查未包含旧 Hook 采集模块。
- `tools/verify_release_package.py --backend npcap` 校验通过，包含 EXE 哈希、版本、资源及更新日志。
- 未发布在线更新，未替换用户正在运行的旧程序。

## 登录白名单已同步

本地登记文件已生成：`build-allowlist-d9ce049bfa3842eeb058535781a1fe62.json`。
用户通过阿里云终端恢复现有公钥后，SSH 已连接成功。2026-09-11 13:35（北京时间）已追加并回读确认：19 条增至 20 条，旧条目完整保留，服务 PID `2589297` 未变化，在线更新元数据未改动。

备份：`/var/lib/gmzz-dps-monitor/build-allowlist.json.pre-v023-20260911T053512Z-2985693`。
白名单 SHA256：`02dacf2e91d0cd1f3c05d298d02b8befb6368be6f9f20a56f0f398dcd0ca81aa`。

`tools/register_build_allowlist.py` 可在服务器以既有管理权限执行：

```sh
python3 register_build_allowlist.py --entry build-allowlist-d9ce049bfa3842eeb058535781a1fe62.json
python3 register_build_allowlist.py --entry build-allowlist-d9ce049bfa3842eeb058535781a1fe62.json --apply
```

工具先核对运行服务使用的路径、运行配置和公钥，再追加单个构建；保留旧条目、备份原文件、原子替换并回读；不发布更新、不重启服务。此操作现已完成。

整场战斗、队友完整性及独立被动对照仍待实测；编译成功不代表这些业务验收已经通过。
