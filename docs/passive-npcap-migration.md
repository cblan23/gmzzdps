# 被动 Npcap 改造进度与验收边界

日期：2026-09-11。当前开发分支：`refactor/passive-npcap`。

最新调整：按用户明确要求，源码主程序和默认正式打包入口已统一切换为 Npcap，不再需要 `-PassiveCapture`。旧 `legacy` 后端配置会明确报错，不允许重新启用 Hook。已安装的旧 EXE 和线上下载包不会因源码变化自动更新，本轮尚未发布替换包。

改造前代码已上传 GitHub：`cblan23/gmzzdps`，提交
`56b80ba71fec84a32ea4703965194b74b7b8ff42`，标签
`baseline/v0.2.3b-before-npcap`。本次不发布线上更新，不改变服务器白名单。

## 已实现

- `npcap_runtime.py`：验证 Npcap 驱动版本；不把 wpcap/libpcap 版本误当驱动版本。
- `npcap_receiver.py`：在 activate 前配置缓冲区、立即交付和时间戳精度；非阻塞轮询多个接口；定期刷新接口和端口；接口失败可重新打开；统计驱动丢包。仅绑定接收 API。
- `npcap_shadow_capture.py`：读取 Windows IPv4/IPv6 UDP 端口归属，处理查询期间端口表大小变化。
- `passive_transport.py`：纯函数式收包入口，Ethernet/VLAN、IPv4/IPv6 分片、长度验证、超时、容量限制、冲突拒绝。TCP 重组覆盖乱序、重传、冲突、回绕、连接代次与缺包超时，但当前游戏接入仍仅使用已验证的 UDP/KCP。
- `npcap_capture_process.py`：接入多接口和分片重组；根据进程端口及本地地址辨别方向，包括回环；KCP 回绕处理和已交付序号去重；场景切换不会直接清空交付历史；保留既有租约鉴权。
- `npcap_protocol.py`：原始整型纳秒时间戳传到 RPC 记录，FILETIME 使用整数换算；增加采集会话、解析代次、KCP 序号和消息偏移构成的事件标识；相同内容的不同技能消息不会按内容去重。事件标识用于采集会话内识别，不是跨客户端或跨重连的全服事件 ID。
- `dps_meter.pyw`：唯一改动是普通被动版沿用 `GMZZDpsMeter` 数据目录。显式隔离版仍可通过构建配置使用独立目录。UI、统计模型、战斗记录格式没有改动。
- `build_exe.ps1`：默认采用被动后端，保留正常版本/数据目录/互斥锁；`-PassiveCapture` 仅作为旧命令兼容参数，原 `-NpcapVariant` 仍用于隔离研究。被动构建排除旧 Hook 模块，并检查编译报告。
- `compare_capture_history.py`：对同一场战斗的两份存档逐字段比较，缺失值不变成零，个人数值不按团队总量相抵。返回双方都缺失的字段，不能据此宣称缺失项目通过验收。

## 本机验证

运行环境安装 Npcap 1.88，wpcap/libpcap 1.10.6。

1. 6 个接收接口成功打开，未出现启用警告，返回纳秒格式时间戳。格式精度不代表服务器事件的实际时间精度。
2. `npcap_transport_probe.py --seconds 15`：480 个收包帧，247 个游戏入站 UDP 数据报，201 个唯一 PUSH 序号，36 次重复观察，已观测序号跨度内没有缺口。
3. `npcap_backend_probe.py --seconds 35`：使用现有显式开发租约启动真正的被动后端；485 个唯一 PUSH，88 次重传，54 条已识别 RPC，解压/JSON 序列化错误均为 0，新增服务器请求为 0。
4. 上述不是完整战斗；未知方法仍有 108 次，尚未确定是否包含需要补充映射的业务事件。取得快照前的 124 个 PUSH 未作为同步后的数据发出，不能宣称中途启动可以恢复之前所有事件。
5. 原程序仍在运行，这些结果不是“无旧链路参与”的完整功能验证。不得用它们宣称队友信息完整或统计准确率已经高于旧版。
6. 完整回归 1,056 项通过。随后增加的私有诊断日志导出和回放输出入口通过语法检查，对比工具 4 项测试通过。

诊断工具默认只输出计数，不保存封包、聊天内容、角色身份或解密密钥。后端诊断会只读游戏 RC4/Zstd 状态；纯传输诊断不会读取这些状态。诊断不会关闭游戏或现有客户端。

## 尚未完成，不能省略

- 同场完整战斗的旧版/新版逐角色总伤、DPS、HPS、DT、技能次数、暴击、Boss 伤害和时间轴对比。
- 旧 Hook 完全退出后的独立被动验证，覆盖胜利、失败、离开、阶段切换、重连、多网卡/VPN 实际切换。
- 分析主动请求停止后哪些队友属性/技能汇总不再自然下发；被动收包不能创造服务器没有下发的数据。现有研究的 Boss 上下文推断不能未经验证直接复制到当前业务解析器。
- Windows TCP 进程连接发现及实际 TCP 应用协议接入。当前 TCP 类是经测试的基础设施，不代表游戏 TCP 业务已经支持。
- 整场缺失范围与完整性标记的业务验收、压力测试、Nuitka 候选包和最终打包检查。源码/默认正式构建的后端现已切换，但上述完整性验证仍未完成，也没有发布。

游戏协议加密并有连续压缩状态。现有可运行方案需要
`PROCESS_QUERY_INFORMATION | PROCESS_VM_READ` 读取连接状态；不写游戏内存、不注入、不 Hook、不发送游戏请求。如果进一步要求完全不读取游戏进程，当前还没有已验证可用的解密方案。

## 复现命令

```powershell
$env:PYTHONPATH=(Resolve-Path '.codex-tmp\python-deps').Path
py -3 -m unittest
py -3 npcap_transport_probe.py --seconds 15
py -3 npcap_backend_probe.py --seconds 35
py -3 compare_capture_history.py legacy-fight.json passive-fight.json
```

为完整战斗留取可回放样本（输出文件必须不存在，目录须已存在）：

```powershell
py -3 npcap_backend_probe.py --seconds 900 --records-output .codex-tmp\passive-fight.jsonl
py -3 replay_combat_log.py .codex-tmp\passive-fight.jsonl --cold-cache --records-output .codex-tmp\passive-encounters.json --anonymous-only
```

`--records-output` 是显式开启的私有诊断输出，可能包含游戏角色身份和战斗数据，但不输出解密状态；不要提交或公开。默认模式仍只输出计数。完整比较需要同时回放对应时间段的旧版日志，按实际同一场选择两侧的 encounter index；不能拿不同战斗或双方均缺数据作为通过依据。

候选构建入口（当前未执行构建、未发布）：

```powershell
.\build_exe.ps1 -OutputDirectory .codex-tmp\npcap-candidate
```

默认源码/构建已采用 Npcap；捕获失败不能静默回退到 Hook。完整性验收结果须如实记录，源码切换不代表所有业务对比已通过，也不代表已发布线上更新。
