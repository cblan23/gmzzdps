"""Shared validation contract for future damage-dummy template IDs."""

from __future__ import annotations


DAMAGE_TARGET_NAME = "伤害木桩"
DAMAGE_TARGET_LOCALIZATION_ID = 435_407_409_907_200
DAMAGE_TARGET_LOCALIZATION_IDS = frozenset(
    {
        391_426_944_796_160,
        422_213_270_373_888,
        DAMAGE_TARGET_LOCALIZATION_ID,
    }
)
# Reserve the next IDs around the current damage-dummy sequence.  An ID in
# this range is only enabled after the installed client's MonsterData proves
# both its BossType and one of the observed damage-dummy localization IDs.
RESERVED_DAMAGE_TARGET_TEMPLATE_IDS = frozenset(range(7_100_635, 7_100_645))


def is_validated_reserved_damage_target(
    template_id: object,
    boss_type: object,
    localization_id: object,
) -> bool:
    try:
        return bool(
            int(template_id) in RESERVED_DAMAGE_TARGET_TEMPLATE_IDS
            and int(boss_type) == 3
            and int(localization_id) in DAMAGE_TARGET_LOCALIZATION_IDS
        )
    except (TypeError, ValueError, OverflowError):
        return False


__all__ = [
    "DAMAGE_TARGET_LOCALIZATION_ID",
    "DAMAGE_TARGET_LOCALIZATION_IDS",
    "DAMAGE_TARGET_NAME",
    "RESERVED_DAMAGE_TARGET_TEMPLATE_IDS",
    "is_validated_reserved_damage_target",
]
