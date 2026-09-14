# 队友战斗数据最小干预实验：执行记录

状态：准备和短时工具预检完成；3～5 分钟真实战斗 A/B 及单次攻击对照尚未执行。本文不是最终可行性结论。

## 约束

本次仅增加独立研究工具，不修改正式采集、统计或线上配置。不新增主动请求，不重放、伪造或修改游戏协议，不开启隐藏 UI 或调用遗留函数。B 组只允许已有正式版的请求频率，不额外安装发送记录 Hook。降低干预优先于字段齐全，缺失字段保留未知。

## 现有链路审计

- `capture_process.py` 安装 `TeamStatsRequestHook(interval=1.0, stable_primary_only=True)`。
- `team_stats_request_hook.py:build_primary_team_request_stub` 在 call_server 入口保存上下文，以时间门限及原子抢占控制额外调用，通过 trampoline 调用原游戏 RPC，随后恢复并继续原请求。不是仅监听 Req；1 秒是调度门限，仍需要游戏线程经过该入口，不等于严格每秒必定成功发包。
- primary-only 路径不安装 outbound recorder。因此正式版日志没有 Req 事件不能证明没有 Req；hook request_count 是额外调用次数，也不等于网卡成功发出的请求数。
- `npcap_protocol.py` 当前解码主要面向 inbound RC4 流。`npcap_capture_process.py` 会通过只读进程访问获取 RC4 和 Zstd 状态，并可能读取实体元数据。不能把该完整后端称为“不进入游戏进程”。
- 旧自然回包探针仍读取解码状态，即使未装 Hook，也不是严格 A 组。仅 S→C 解码不能报告 C→S Req=0。
- 同一字段编号在不同消息含义不同，例如角色属性的字段 27 可为评分，不能全局解释为命中次数。

## 待跟踪消息

现有静态方法映射为版本/实体类作用域内的证据，不能把未知实体的相同数字套用为玩家 RPC：

| 消息 | 已有编号/来源 | 重点 |
| --- | --- | --- |
| RetCommonCombatStatisticsByTeam | 338 | 团队累计值 |
| RetDirtyCommonCombatStatisticsByTeam | LocalRole 339 | 验证请求触发还是自然推送；Dirty 名称不等于数值增量 |
| RetCommonCombatStatistics | LocalRole 340 | 独立个人详情 |
| OnMsgUpdateStageCombatStatistics | LocalRole 341 | 阶段汇总 |
| OnMsgSettlementCombatStatistics | 342 | 结算汇总 |
| RetDungeonBattleStatistics / RetMonsterBattleStatistics | LocalRole 328/329 | 旧统计系统 |
| OnMsgUpdateDungeonBattleStatistics / OnMsgUpdateDungeonTeamPlayerBattleStatistics | LocalRole 330/331 | 更新候选 |
| RetNpcCombatStatisticsByTeam / RetDirtyNpcCombatStatisticsByTeam / RetNpcCombatStatistics | LocalRole 335/336/337 | 按目标及详情候选 |

详情/结算确认的字段包括 34 技能伤害、33 技能次数、25 暴击命中、27 命中、14 未穿刺命中；字段需按方法/表结构解析并校验合法计数。Common 累计伤害与结算总伤字段编号不同，不能混用。

既有字符串中有 DungeonBattleStatisticsSender/Receiver/Model/System 和 BattleStatisticsPanelClass。只证明存在遗留名称，不证明公测可用入口或请求原型。尚未完成“团队总览→点击队员”的静态调用链确认；不为验证而调用该入口。

## 新增研究工具

- `tools/teamstats_passive_experiment.py`：Npcap 全包长、双向 UDP、所有当前游戏 PID 拥有端口，多网卡、时间戳精度、IP 重组、KCP 序列/长度/hash、原始帧、驱动和队列诊断；独占新文件，不覆盖旧证据，最长 300 秒、默认 256 MiB 上限，支持 stop-file 正常结束。无游戏进程句柄、内存读取、主动 socket 发送或 Npcap 发送调用。
- `tools/teamstats_message_audit.py`：离线逻辑消息原文与递归字段差异，保留时间、源、opcode、entity、KCP 序列。缺失字段不是零也不是删除；变化不是 Delta 协议语义证明。不删除内容相同的真实事件。
- `tools/test_teamstats_message_audit.py`：3 项通过。

预检 8.032 秒：249 帧，入站 125 / 8778 UDP payload 字节，出站 124 / 3191 字节；Npcap 驱动和缓冲队列未报告丢弃。正式版同时运行，group=preflight，不计作 A/B 战斗证据。

旧战争巨龙样本离线复核：Common 50、结算 1、DamageSync 38、BeatenSync 110、CastSkill 34；旧请求器并行，不是纯被动自然推送证据。

## 必须补齐的观测边界

当前新记录器只覆盖 UDP。当前游戏另有已建立 TCP、HTTPS 和本地代理连接，因此不能排除其他通道。已查询本机 Pktmon 帮助并确认当前未运行；下一次正式实验需用 Pktmon 针对游戏端点补充 TCP/UDP 完整数据包，检查过滤器与新连接覆盖，保留重复组件捕获信息，不擅自清理用户已有过滤器。

原始帧可能包含账号/会话信息，保留本地私有目录，不上传公共服务，不在报告中公开原始载荷或密钥。

严格 A 组不能同时安装 outbound 观察 Hook；若 C→S 加密尚不能解码，Req 数量必须写“未知”。不得从零条可读 Req 推导“自然 Req 为零”。

解码辅助可以另设 A'：保持无主动请求，但明确使用只读解码状态引导。A' 与不打开游戏进程的 A 分开标注；即使 A' 可还原全部字段，也不能宣布目标纯网络解码已完成。启动 A' 前需向用户说明边界。

## 待执行真实战斗流程

1. 用户准备同类副本与可配合真人队友，记录场景、难度、成员及版本。退出统计工具前等待适当时机，正常关闭 Hook 程序，确认子进程清理。读入口原字节验证作为实验前独立审计记录，不放进 A 采集进程，也不伪称完全未访问过进程。
2. A：预留静默期区分旧请求迟到回包，再完整 180～300 秒采集。记录进队、开战、结束的人工标记。队友停手10秒→单次攻击→停手10秒，至少3次，注明自动被动/DOT/宠物造成的非单击污染。
3. B：仅恢复当前正式客户端，重复同类战斗与操作；同期开启被动原始记录，与现有内部日志按 FILETIME、KCP序列、实体、字段关联。时间接近只是候选，不能代替事件内容一致性。
4. 分析自然 Req/Full/Dirty、已确认伤害事件、技能/命中字段覆盖及丢包窗口。窗口、起止、请求可观测性、解码失败和未知消息数一起报告。
5. 覆盖胜利、团灭、退出才可讨论每场保障；单次有/无回包不能推断全部场景。

## 当前 A～F 状态（不是实验最终判定）

- A/B：未证实。需要自然请求及持续推送实测。
- C：主动额外调用已由代码证实，但“只有主动请求才有持续统计”尚未由严格 A/B 证明。
- D：部分伤害/施法消息在已有网络样本可见；全队逐击覆盖未证实。
- E：既有样本证实团队回包可从网卡捕获数据经解码还原；完全不进入游戏进程获取解码条件尚未实现。
- F：不能判定。缺失解码能力或短时无回包不能证明网络根本不存在所需数据。

正式重构、删除 Hook 和调整请求频率均未实施。真实试验完成前不能承诺可删除多少 Hook 或保证核心 DPS 完整性。

## 本轮继续：预检与分组控制

- Pktmon 脚本原先在 Windows PowerShell 5 中因 UTF-8 无 BOM 的中文匹配字面量被误解码，误报无法确认停止状态。已改为码点构造匹配，仍保留“不影响已有抓包/过滤器”的检查。
- 5 秒 Pktmon 预检实际启动成功，记录16个游戏拥有的UDP/TCP端口，导出1369条包记录。停止报告“无事件丢失”；转换报告2条丢弃事件（网络栈丢弃事件，不等于捕获器丢2条事件）。原ETL保留全部组件身份，PCAPNG包含多组件重复观察，不能直接将1369当成独立网络包或技能次数。
- 预检结束后已确认 Pktmon 停止，过滤器为空。数据位于 `.codex-tmp/teamstats-experiment/pktmon-preflight-r2`。后续脚本另存 capture-status.txt，保留原生启动/结束/转换计数。
- 新增 `tools/audit_capture_hook_entries.py`：独立只读检查已配置的8个入口，保存是否匹配原始序言，不写游戏内存，不输出地址或内容。它不是A组数据源，也不能证明未知位置绝无其他工具Hook。
- 本轮当前游戏 PID21836，正式包仍运行时，team_stats入口恰好为original，另外7个配置入口不匹配原始序言。因此“正式版开着”不等于主动TeamStats持续运行；需要结合场景模式和实际request_count验证B组。此刻的入口状态也不能证明过去没发过请求。
- 未自动开始3～5分钟有效战斗，不把无人协调的空闲窗口冒充A/B实战。需要用户确认真人队友、同类副本、停手/单击阶段后，再关闭正式采集并开始计时。
- 严格A组无解码条件时自然Req数量仍为未知。A'只读解码引导不混入A；自然回包少不意味着已证明F。
