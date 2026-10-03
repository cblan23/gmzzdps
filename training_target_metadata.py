"""Shared validation contract for future damage-dummy template IDs."""

from __future__ import annotations


DAMAGE_TARGET_NAME = "伤害木桩"
DAMAGE_TARGET_LOCALIZATION_ID = 422_213_270_373_888
# Reserve the next ten IDs after the current 84-level dummy.  An ID in this
# range is only enabled after the current client's MonsterData validates it.
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
            and int(localization_id) == DAMAGE_TARGET_LOCALIZATION_ID
        )
    except (TypeError, ValueError, OverflowError):
        return False


__all__ = [
    "DAMAGE_TARGET_LOCALIZATION_ID",
    "DAMAGE_TARGET_NAME",
    "RESERVED_DAMAGE_TARGET_TEMPLATE_IDS",
    "is_validated_reserved_damage_target",
]
