"""Scoped RPC names read from the current c7-2026-09-02 method descriptors.

IDs belong to entity classes. Do not apply the player table to an NPC just
because an integer matches. Tables contain names, never captured game values.
"""

LOCAL_ROLE_RPC_METHODS = {
    201: 'OnSyncTeamGroupInfo',
    203: 'OnCreateGroupSuccess',
    204: 'OnJoinGroupSuccess',
    208: 'OnMsgQuitTeamGroup',
    209: 'OnMsgOtherQuitTeam',
    216: 'OnUpdateTeamGroupDetail',
    217: 'OnMsgSyncWorldReturnInfo',
    218: 'OnMsgClearWorldReturnInfo',
    227: 'OnMsgMembersJoinGroup',
    229: 'OnMsgMembersJoinTeam',
    249: 'OnMsgTeamGroupDisbanded',
    250: 'OnSyncTeamQuitGroup',
    265: 'OnMsgOtherTeamQuitGroup',
    272: 'OnSyncTeamGroupPropsForceRefresh',
    328: 'RetDungeonBattleStatistics',
    329: 'RetMonsterBattleStatistics',
    330: 'OnMsgUpdateDungeonBattleStatistics',
    331: 'OnMsgUpdateDungeonTeamPlayerBattleStatistics',
    335: 'RetNpcCombatStatisticsByTeam',
    336: 'RetDirtyNpcCombatStatisticsByTeam',
    337: 'RetNpcCombatStatistics',
    339: 'RetDirtyCommonCombatStatisticsByTeam',
    340: 'RetCommonCombatStatistics',
    341: 'OnMsgUpdateStageCombatStatistics',
    343: 'OnMsgReconnectOrEnter',
    344: 'OnMsgRemoveAvatarsCombatStatistics',
    381: 'OnMsgBeforeEnterNewSpace',
    1381: 'OnMsgLeaveQuestControl',
    1541: 'RetStopEditTarotTeamSceneCustom',
    2239: 'OnMsgDisableEpoll',
    2242: 'OnMsgLeaveSpace',
    2244: 'OnMsgEntityDead',
    2245: 'OnMsgEntityRelive',
    2253: 'RetNTP',
}

NPC_RPC_METHODS = {253: 'OnMsgEntityDead'}
