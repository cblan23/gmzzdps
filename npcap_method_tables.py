"""Scoped RPC names read from the current c7-2026-09-17 method descriptors.

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
}

NPC_RPC_METHODS = {253: 'OnMsgEntityDead'}
