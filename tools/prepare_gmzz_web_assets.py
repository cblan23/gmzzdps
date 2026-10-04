"""Copy the extracted client icons and build the website's small asset catalog."""
from __future__ import annotations

import json
from pathlib import Path
import shutil


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / 'assets'
    target = root / 'web/gmzz/assets'
    skills = {}
    for group in ('skills', 'equipment', 'professions'):
        (target / group).mkdir(parents=True, exist_ok=True)
        for path in sorted((source / group).glob('*.png')):
            shutil.copyfile(path, target / group / path.name)
            if group == 'skills':
                skills[path.stem] = path.name
    manifest = json.loads((source / 'equipment/manifest.json').read_text(encoding='utf-8'))
    icons = {key: row['file'] for key, row in manifest['icons'].items()
             if (target / 'equipment' / row['file']).is_file()}
    catalog = {'skills': skills, 'equipment': icons, 'items': manifest['items'],
               'attributes': {
                   'pAtkMin': '物理攻击', 'mAtkMin': '非凡攻击', 'pDef': '物理防御', 'mDef': '非凡防御',
                   'pCrit': '物理暴击', 'mCrit': '非凡暴击', 'pCritAnti': '物理抗暴', 'mCritAnti': '非凡抗暴',
                   'pPierce': '物理穿透', 'mPierce': '非凡穿透', 'pBlock': '物理格挡', 'mBlock': '非凡格挡',
                   'pDefReduce': '物理防御削减', 'mDefReduce': '非凡防御削减', 'AirShield': '护盾',
                   'ShieldBreak': '破盾', 'SkillPlus': '技能增伤', 'BeSkilledReduce': '受技能伤害降低',
                   'Atk_N': '攻击力', 'Crit_N': '暴击', 'Def_N': '防御', 'Pierce_N': '穿透',
                   'Block_N': '格挡', 'MaxHp_N': '最大生命', 'CritAnti_N': '抗暴',
                   'DefReduce_N': '防御削减', 'AirShield_N': '护盾', 'ShieldBreak_N': '破盾',
                   'SkillPlus_N': '技能增伤', 'BeSkilledReduce_N': '受技能伤害降低',
               }}
    (target / 'icon_catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(json.dumps({'skills': len(skills), 'equipment': len(icons), 'items': len(manifest['items'])}))


if __name__ == '__main__':
    main()
