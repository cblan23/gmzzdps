# BOSS 头像清单

生成时间：`2026-09-05T22:13:38+08:00`

## 覆盖结论

- 当前历史记录：23 个文件，14 个 BOSS/机制模板，全部已映射。
- 当前应用识别目录：84 个模板，83 个可显示头像。
- 历史外可靠目录：38 个模板已接入运行时回退；以后首次写入历史记录也能直接对应头像。
- 另有 3 个“阿蒙”同名模板可在记录包含 `StageId` 时按关卡确定头像，缺少关卡上下文时不会乱配。
- 客户端副本配置：32 个 Dungeon、101 个 Stage、33 张唯一 Stage 原图。
- 正式素材：36 张 PNG。唯一仍无客户端头像的是任务首领“梦境捕手”（模板 `7265156`）。

同一 BOSS 的普通、困难或机制模板会共用客户端原图；这里保证模板可确定查图，不宣称每个模板都有不同图片。

## 点名副本

### 黑荆棘事件簿

| StageId | 首领/阶段 | 头像 |
| ---: | --- | --- |
| `5150001` | 朗伯·绞索 | [lambert-noose.png](lambert-noose.png) |
| `5150002` | 每日事件 | [random-stage.png](random-stage.png) |
| `5150003` | 卡尔·埃德加 | [karl-edgar.png](karl-edgar.png) |
| `5150005` | 亚巴顿 | [head-enemy-04.png](head-enemy-04.png) |
| `5150004` | 洛克·金 | [rock-king.png](rock-king.png) |

### 安提哥努斯笔记

| StageId | 首领/阶段 | 头像 |
| ---: | --- | --- |
| `5150038` | 护送马车 | [head-enemy-01.png](head-enemy-01.png) |
| `5150039` | 瑞尔比伯 | [head-enemy-02.png](head-enemy-02.png) |
| `5150040` | 小丑 | [clown.png](clown.png) |
| `5150047` | 护送马车 | [head-enemy-01.png](head-enemy-01.png) |
| `5150048` | 瑞尔比伯 | [head-enemy-02.png](head-enemy-02.png) |
| `5150049` | 小丑 | [clown.png](clown.png) |

### 五月庄园·花园

| StageId | 首领/阶段 | 头像 |
| ---: | --- | --- |
| `5150050` | 异化猎犬 | [mutated-hound.png](mutated-hound.png) |
| `5150051` | 先祖铠甲 | [ancestral-knight.png](ancestral-knight.png) |
| `5150052` | 星象仪者 | [astrologer.png](astrologer.png) |
| `5150058` | 异化猎犬 | [mutated-hound.png](mutated-hound.png) |
| `5150059` | 先祖铠甲 | [ancestral-knight.png](ancestral-knight.png) |
| `5150060` | 星象仪者 | [astrologer.png](astrologer.png) |

### 五月庄园·城堡

| StageId | 首领/阶段 | 头像 |
| ---: | --- | --- |
| `5150053` | 子嗣守护 | [descendant-guardian.png](descendant-guardian.png) |
| `5150054` | 一号信徒 | [yhxt-stage.png](yhxt-stage.png) |
| `5150055` | 子爵夫人 | [viscountess.png](viscountess.png) |
| `5150061` | 子嗣守护 | [descendant-guardian.png](descendant-guardian.png) |
| `5150062` | 一号信徒 | [yhxt-stage.png](yhxt-stage.png) |
| `5150063` | 子爵夫人 | [viscountess.png](viscountess.png) |

## 当前应用 BOSS 模板

| TemplateId | 名称 | 头像 | 映射类型 | 当前历史 |
| ---: | --- | --- | --- | :---: |
| `7100004` | 瑞尔·比伯 | [head-enemy-02.png](head-enemy-02.png) | `stage_shared` | 否 |
| `7100008` | 小丑 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7100201` | 安西娅 | [anxia.png](anxia.png) | `candidate` | 否 |
| `7100202` | 巴尼先生 | [mr-barney.png](mr-barney.png) | `candidate` | 否 |
| `7100203` | 安西娅 | [anxia.png](anxia.png) | `candidate` | 否 |
| `7100208` | 安西娅 | [anxia.png](anxia.png) | `candidate` | 是 |
| `7100209` | 巴尼先生 | [mr-barney.png](mr-barney.png) | `candidate` | 是 |
| `7100210` | 安西娅 | [anxia.png](anxia.png) | `candidate` | 是 |
| `7100215` | 子嗣守护 | [descendant-guardian.png](descendant-guardian.png) | `direct` | 是 |
| `7100625` | 战争巨龙 | [war-dragon.png](war-dragon.png) | `direct` | 否 |
| `7100628` | 战争巨龙 | [war-dragon.png](war-dragon.png) | `direct` | 否 |
| `7100632` | 伤害木桩 | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7101004` | 伤害木桩 | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7101017` | 战场木桩怪 | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7101018` | 战场小丑 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7101025` | 伤害木桩(免疫位移） | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7101026` | 周本-瑞尔比伯 | [head-enemy-02.png](head-enemy-02.png) | `stage_shared` | 否 |
| `7101027` | 英雄周本-瑞尔比伯 | [head-enemy-02.png](head-enemy-02.png) | `stage_shared` | 否 |
| `7101029` | 伤害木桩(匀速移动） | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7101030` | 伤害木桩(闪避移动） | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7102030` | 瑞尔·比伯 | [head-enemy-02.png](head-enemy-02.png) | `stage_shared` | 否 |
| `7102046` | 瑞尔·比伯 | [head-enemy-02.png](head-enemy-02.png) | `stage_shared` | 否 |
| `7102047` | 小丑 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7102048` | 小丑 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7102400` | 星象仪者 | [astrologer.png](astrologer.png) | `direct` | 否 |
| `7102401` | 禁锢 | [astrologer.png](astrologer.png) | `encounter_auxiliary_shared` | 否 |
| `7102402` | 星光守卫 | [astrologer.png](astrologer.png) | `encounter_auxiliary_shared` | 否 |
| `7102403` | 星象仪者 | [astrologer.png](astrologer.png) | `direct` | 是 |
| `7102404` | 禁锢 | [astrologer.png](astrologer.png) | `encounter_auxiliary_shared` | 是 |
| `7102405` | 星光守卫 | [astrologer.png](astrologer.png) | `encounter_auxiliary_shared` | 是 |
| `7102834` | 瑞尔·比伯 | [head-enemy-02.png](head-enemy-02.png) | `stage_shared` | 否 |
| `7102835` | 小丑 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7102873` | 小丑 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7102932` | 子爵夫人 | [viscountess.png](viscountess.png) | `direct` | 否 |
| `7102936` | 子爵夫人-神话姿态 | [viscountess.png](viscountess.png) | `direct` | 否 |
| `7102938` | 子嗣守护 | [descendant-guardian.png](descendant-guardian.png) | `direct` | 否 |
| `7102980` | 一号信徒 | [yhxt-stage.png](yhxt-stage.png) | `direct` | 否 |
| `7102990` | 子爵夫人 | [viscountess.png](viscountess.png) | `direct` | 否 |
| `7102991` | 子爵夫人-神话姿态 | [viscountess.png](viscountess.png) | `direct` | 否 |
| `7103001` | 瑞尔·比伯 | [head-enemy-02.png](head-enemy-02.png) | `stage_shared` | 否 |
| `7103008` | 小丑 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7103012` | 小丑 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7103206` | 伯德温·威瑟尔 | [ancestral-knight.png](ancestral-knight.png) | `stage_shared` | 否 |
| `7103210` | 先祖铠甲 | [ancestral-knight.png](ancestral-knight.png) | `stage_shared` | 否 |
| `7103301` | 瑞尔比伯 | [head-enemy-02.png](head-enemy-02.png) | `stage_shared` | 否 |
| `7103309` | 小丑 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7103312` | 小丑分身 | [clown.png](clown.png) | `stage_shared` | 否 |
| `7103401` | 伯德温·威瑟尔 | [ancestral-knight.png](ancestral-knight.png) | `stage_shared` | 是 |
| `7103402` | 先祖铠甲 | [ancestral-knight.png](ancestral-knight.png) | `stage_shared` | 是 |
| `7106130` | 子爵夫人 | [viscountess.png](viscountess.png) | `direct` | 否 |
| `7107030` | 卡尔·埃德加 | [karl-edgar.png](karl-edgar.png) | `direct` | 否 |
| `7107081` | 卡尔·埃德加 | [karl-edgar.png](karl-edgar.png) | `direct` | 否 |
| `7107105` | 朗伯·绞索 | [lambert-noose.png](lambert-noose.png) | `stage_shared` | 是 |
| `7107121` | 冰牢 | [lambert-noose.png](lambert-noose.png) | `encounter_auxiliary_shared` | 否 |
| `7107352` | 亚巴顿 | [head-enemy-04.png](head-enemy-04.png) | `stage_shared` | 否 |
| `7107450` | 亚巴顿 | [head-enemy-04.png](head-enemy-04.png) | `stage_shared` | 否 |
| `7109004` | 瑞尔·比伯（精英） | [head-enemy-02.png](head-enemy-02.png) | `stage_shared` | 否 |
| `7109005` | 小丑（精英） | [clown.png](clown.png) | `stage_shared` | 否 |
| `7109801` | 异化猎犬 | [mutated-hound.png](mutated-hound.png) | `direct` | 否 |
| `7109821` | 异化猎犬 | [mutated-hound.png](mutated-hound.png) | `direct` | 是 |
| `7110581` | 洛克·金 | [rock-king.png](rock-king.png) | `direct` | 否 |
| `7110641` | 洛克·金·失控 | [rock-king.png](rock-king.png) | `direct` | 否 |
| `7110642` | 洛克·金 | [rock-king.png](rock-king.png) | `direct` | 否 |
| `7110810` | “钻头” | [drill-memory.png](drill-memory.png) | `direct` | 否 |
| `7110817` | “钻头” | [drill-memory.png](drill-memory.png) | `direct` | 否 |
| `7110820` | "剥面人" 强尼 | [johnny-memory.png](johnny-memory.png) | `direct` | 否 |
| `7110825` | "剥面人" 强尼 | [johnny-memory.png](johnny-memory.png) | `direct` | 否 |
| `7111001` | 邦尼 | [bunny-memory.png](bunny-memory.png) | `direct` | 否 |
| `7114223` | 伤害木桩 | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7114225` | 伤害木桩 | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7114227` | 伤害木桩 | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7114233` | 伤害木桩 | [training-dummy.png](training-dummy.png) | `client_ui_shared` | 否 |
| `7115020` | "剥面人" 强尼 | [johnny-memory.png](johnny-memory.png) | `direct` | 否 |
| `7115026` | "剥面人" 强尼 | [johnny-memory.png](johnny-memory.png) | `direct` | 是 |
| `7115040` | 战争巨龙 | [war-dragon.png](war-dragon.png) | `direct` | 是 |
| `7115041` | 战争巨龙 | [war-dragon.png](war-dragon.png) | `direct` | 否 |
| `7115042` | 战争巨龙 | [war-dragon.png](war-dragon.png) | `direct` | 否 |
| `7115060` | 邦尼 | [bunny-memory.png](bunny-memory.png) | `direct` | 否 |
| `7115075` | 邦尼 | [bunny-memory.png](bunny-memory.png) | `direct` | 是 |
| `7115080` | “钻头” | [drill-memory.png](drill-memory.png) | `direct` | 否 |
| `7115090` | “钻头” | [drill-memory.png](drill-memory.png) | `direct` | 否 |
| `7115300` | 邦尼 | [bunny-memory.png](bunny-memory.png) | `direct` | 否 |
| `7115310` | 邦尼 | [bunny-memory.png](bunny-memory.png) | `direct` | 否 |
| `7265156` | 梦境捕手 | 无客户端头像 | `unavailable` | 否 |

## 历史外客户端模板

这些模板尚未进入当前应用静态识别目录，但已能由客户端唯一 Stage 头像预先对应。

| TemplateId | 名称 | 头像 | 映射类型 |
| ---: | --- | --- | --- |
| `7100013` | 杰森 | [lambert-noose.png](lambert-noose.png) | `client_stage_name` |
| `7100058` | 绯红意志 | [head-enemy-08.png](head-enemy-08.png) | `client_stage_name` |
| `7100301` | 厄水巨龟 | [evil-water-turtle.png](evil-water-turtle.png) | `client_stage_name` |
| `7100341` | 舞王狒哥 | [dance-king-baboon.png](dance-king-baboon.png) | `client_stage_name` |
| `7100361` | 舞王狒哥 | [dance-king-baboon.png](dance-king-baboon.png) | `client_stage_name` |
| `7100371` | 西尔维娅 | [sylvia.png](sylvia.png) | `client_stage_name` |
| `7100401` | 厄水巨龟 | [evil-water-turtle.png](evil-water-turtle.png) | `client_stage_name` |
| `7100441` | 舞王狒哥 | [dance-king-baboon.png](dance-king-baboon.png) | `client_stage_name` |
| `7100471` | 西尔维娅 | [sylvia.png](sylvia.png) | `client_stage_name` |
| `7100472` | 西尔维娅 | [sylvia.png](sylvia.png) | `client_stage_name` |
| `7101021` | 绯红意志 | [head-enemy-08.png](head-enemy-08.png) | `client_stage_name` |
| `7102010` | 泰尔 | [head-enemy-05.png](head-enemy-05.png) | `client_stage_name` |
| `7102011` | 史蒂夫 | [head-enemy-04.png](head-enemy-04.png) | `client_stage_name` |
| `7102033` | 绯红意志 | [head-enemy-08.png](head-enemy-08.png) | `client_stage_name` |
| `7102063` | 绯红意志 | [head-enemy-08.png](head-enemy-08.png) | `client_stage_name` |
| `7102096` | 绯红意志 | [head-enemy-08.png](head-enemy-08.png) | `client_stage_name` |
| `7104820` | 佛尔思 | [sasriel.png](sasriel.png) | `client_stage_name` |
| `7104871` | 佛尔思 | [sasriel.png](sasriel.png) | `client_stage_name` |
| `7105900` | 苹果骑士 | [sasriel.png](sasriel.png) | `client_stage_name` |
| `7107300` | 史蒂夫 | [head-enemy-04.png](head-enemy-04.png) | `client_stage_name` |
| `7108005` | 阿蒙分身 | [amon-clone.png](amon-clone.png) | `reviewed_alias` |
| `7108100` | 咕噜 | [goulu.png](goulu.png) | `client_stage_name` |
| `7108150` | 阿蒙分身 | [amon-clone.png](amon-clone.png) | `reviewed_alias` |
| `7108200` | 乌黯魔狼 | [dark-wolf.png](dark-wolf.png) | `client_stage_name` |
| `7108250` | 梅迪奇残影 | [medici-remnant.png](medici-remnant.png) | `reviewed_alias` |
| `7108300` | 米尔贡根 | [milgongen.png](milgongen.png) | `client_stage_name` |
| `7108301` | 米尔贡根 | [milgongen.png](milgongen.png) | `client_stage_name` |
| `7108350` | 萨斯利尔 | [sasriel.png](sasriel.png) | `client_stage_name` |
| `7109100` | 咕噜 | [goulu.png](goulu.png) | `client_stage_name` |
| `7109200` | 阿蒙分身 | [amon-clone.png](amon-clone.png) | `reviewed_alias` |
| `7109400` | 梅迪奇残影 | [medici-remnant.png](medici-remnant.png) | `reviewed_alias` |
| `7109500` | 米尔贡根 | [milgongen.png](milgongen.png) | `client_stage_name` |
| `7109501` | 米尔贡根 | [milgongen.png](milgongen.png) | `client_stage_name` |
| `7109600` | 萨斯利尔 | [sasriel.png](sasriel.png) | `client_stage_name` |
| `7109900` | 乌黯魔狼 | [dark-wolf.png](dark-wolf.png) | `client_stage_name` |
| `7110200` | 罗塞尔的残留意志 | [roselle-will.png](roselle-will.png) | `reviewed_alias` |
| `7110208` | 罗塞尔的残留意志 | [roselle-will.png](roselle-will.png) | `reviewed_alias` |
| `7190025` | 绯红意志 | [head-enemy-08.png](head-enemy-08.png) | `client_stage_name` |

## 需要关卡上下文的同名模板

以下同名模板在客户端配置中对应多张 Stage 头像，必须结合 `stage_id` 或 `dungeon_id`，不能仅按名称选图。

| TemplateId | 名称 | 候选头像 |
| ---: | --- | --- |
| `7102034` | 阿蒙 | [head-enemy-08.png](head-enemy-08.png)、[lambert-noose.png](lambert-noose.png) |
| `7102037` | 阿蒙 | [head-enemy-08.png](head-enemy-08.png)、[lambert-noose.png](lambert-noose.png) |
| `7102059` | 阿蒙 | [head-enemy-08.png](head-enemy-08.png)、[lambert-noose.png](lambert-noose.png) |
