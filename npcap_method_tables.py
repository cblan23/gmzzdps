"""Scoped RPC names read from verified C7 method descriptors.

IDs belong to entity classes. Do not apply the player table to an NPC just
because an integer matches. Tables contain names, never captured game values.
"""

LOCAL_ROLE_RPC_METHODS = {
    # Team lifecycle and member properties. Other entity classes reuse low
    # numeric IDs, so these names must stay in the LocalRole-scoped table.
    208: 'OnSyncTeamGroupInfo',
    209: 'OnCreateTeamSuccess',
    210: 'OnCreateGroupSuccess',
    211: 'OnJoinGroupSuccess',
    213: 'OnMsgNewTeamGroupApply',
    215: 'OnMsgQuitTeamGroup',
    216: 'OnMsgOtherQuitTeam',
    219: 'OnUpdateTeamGroupMemberProps',
    220: 'OnUpdateTeamGroupSelfProps',
    221: 'OnMsgOtherJoinTeamGroup',
    223: 'OnUpdateTeamGroupDetail',
    224: 'OnMsgSyncWorldReturnInfo',
    225: 'OnMsgClearWorldReturnInfo',
    233: 'OnMsgTeamJoinGroup',
    234: 'OnMsgMembersJoinGroup',
    236: 'OnMsgMembersJoinTeam',
    256: 'OnMsgTeamGroupDisbanded',
    257: 'OnSyncTeamQuitGroup',
    272: 'OnMsgOtherTeamQuitGroup',
    278: 'OnMsgSyncTeamGroupMemberFreqProp',
    279: 'OnSyncTeamGroupPropsForceRefresh',
    # Combat statistics. These callbacks feed the existing settlement parser;
    # only their descriptor indexes changed in the client update.
    335: 'RetDungeonBattleStatistics',
    336: 'RetMonsterBattleStatistics',
    337: 'OnMsgUpdateDungeonBattleStatistics',
    338: 'OnMsgUpdateDungeonTeamPlayerBattleStatistics',
    342: 'RetNpcCombatStatisticsByTeam',
    343: 'RetDirtyNpcCombatStatisticsByTeam',
    344: 'RetNpcCombatStatistics',
    345: 'RetCommonCombatStatisticsByTeam',
    346: 'RetDirtyCommonCombatStatisticsByTeam',
    347: 'RetCommonCombatStatistics',
    348: 'OnMsgUpdateStageCombatStatistics',
    349: 'OnMsgSettlementCombatStatistics',
    350: 'OnMsgReconnectOrEnter',
    351: 'OnMsgRemoveAvatarsCombatStatistics',
    # Verified from the running 2026-10-16 LocalRole descriptors.
    353: 'RetNpcCombatStatisticsByTeam',
    354: 'RetDirtyNpcCombatStatisticsByTeam',
    355: 'RetNpcCombatStatistics',
    356: 'RetCommonCombatStatisticsByTeam',
    357: 'RetDirtyCommonCombatStatisticsByTeam',
    358: 'RetCommonCombatStatistics',
    359: 'OnMsgUpdateStageCombatStatistics',
    360: 'OnMsgSettlementCombatStatistics',
    389: 'OnMsgBeforeEnterNewSpace',
    # Verified LocalRole descriptor indexes from the 2026-09-17 client.
    # PVP has its own passive consumer; these never query TeamStats.
    848: 'OnMsgAddFightRelationship',
    849: 'OnMsgDelFightRelationship',
    850: 'OnMsgSyncBattleType',
    1058: 'RetIndividualPVP',
    1059: 'OnMsgIndividualPVP',
    1060: 'RetIndividualPVPResponse',
    1061: 'OnMsgIndividualPVPResponse',
    1062: 'OnMsgIndividualPVPState',
    1063: 'OnMsgIndividualPVPResult',
    1064: 'OnMsgIndividualPVPLeave',
    1441: 'OnMsgLeaveQuestControl',
    1603: 'RetStopEditTarotTeamSceneCustom',
    2336: 'OnMsgDisableEpoll',
    2339: 'OnMsgLeaveSpace',
    2341: 'OnMsgEntityDead',
    2342: 'OnMsgEntityRelive',
    2350: 'RetNTP',
    2451: 'OnMsgEntityDead',
    2452: 'OnMsgEntityRelive',
    2460: 'RetNTP',
}


# Read-only descriptor scan of the running build 2307645 on 2026-11-12.  These
# IDs overlap both older LocalRole callbacks and other entity classes, so the
# protocol decoder must validate the registered argument shape before using
# this table.  Keep the earlier table above for old captures and clients.
CURRENT_LOCAL_ROLE_RPC_METHODS = {
    # Team lifecycle and member properties.
    218: 'OnSyncTeamGroupInfo',
    219: 'OnCreateTeamSuccess',
    220: 'OnCreateGroupSuccess',
    221: 'OnJoinGroupSuccess',
    223: 'OnMsgNewTeamGroupApply',
    225: 'OnMsgQuitTeamGroup',
    226: 'OnMsgOtherQuitTeam',
    229: 'OnUpdateTeamGroupMemberProps',
    230: 'OnUpdateTeamGroupSelfProps',
    231: 'OnMsgOtherJoinTeamGroup',
    233: 'OnUpdateTeamGroupDetail',
    243: 'OnMsgTeamJoinGroup',
    266: 'OnMsgTeamGroupDisbanded',
    267: 'OnSyncTeamQuitGroup',
    282: 'OnMsgOtherTeamQuitGroup',
    288: 'OnMsgSyncTeamGroupMemberFreqProp',
    289: 'OnSyncTeamGroupPropsForceRefresh',
    # Combat statistics and equipment responses.
    349: 'RetDungeonBattleStatistics',
    350: 'RetMonsterBattleStatistics',
    351: 'OnMsgUpdateDungeonBattleStatistics',
    352: 'OnMsgUpdateDungeonTeamPlayerBattleStatistics',
    356: 'RetNpcCombatStatisticsByTeam',
    357: 'RetDirtyNpcCombatStatisticsByTeam',
    358: 'RetNpcCombatStatistics',
    359: 'RetCommonCombatStatisticsByTeam',
    360: 'RetDirtyCommonCombatStatisticsByTeam',
    361: 'RetCommonCombatStatistics',
    362: 'OnMsgUpdateStageCombatStatistics',
    363: 'OnMsgSettlementCombatStatistics',
    364: 'OnMsgReconnectOrEnter',
    365: 'OnMsgRemoveAvatarsCombatStatistics',
    366: 'RetOtherRoleShapeData',
    # Scene and local-role identity evidence used to scope the callbacks above.
    407: 'OnMsgBeforeEnterNewSpace',
    2534: 'RetNTP',
}


# Actor callback IDs from the same build.  Several actor classes expose a
# compact low-numbered variant, so these names are also shape-gated.
CURRENT_ACTOR_RPC_METHODS = {
    34: 'OnMsgAddBuffNew',
    120: 'OnMsgActorBuffStateSync',
    566: 'OnMsgActorBuffStateSync',
    1206: 'OnMsgAddBuffNew',
    1259: 'OnMsgCastSkillNew',
    1261: 'RetCastSkillSuccessNew',
    1267: 'OnMsgSkillEnterTeamGCD',
}

NPC_RPC_METHODS = {
    253: 'OnMsgEntityDead',
    267: 'OnMsgEntityDead',  # Verified current NpcActor descriptor.
}
