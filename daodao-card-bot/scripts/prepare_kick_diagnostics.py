"""Prepare an offline, bounded diagnostic patch; do not activate or restart QQ."""
import argparse,hashlib
from pathlib import Path

def patch_source(source):
    anchor='      const i = `[KickedOffLine] [${r.tipsTitle}] ${r.tipsDesc}`;'
    assert source.count(anchor)==1,'Unknown upstream shape; refusing patch'
    replacement='''      const daodaoKick = {};
      for (const key of ["kickedType", "securityKickedType", "appId", "instanceId"]) {
        if (Number.isSafeInteger(r[key])) daodaoKick[key] = r[key];
      }
      if (typeof r.sameDevice === "boolean") daodaoKick.sameDevice = r.sameDevice;
      try {
        const path = "/app/napcat/config/daodao-kick-events.jsonl";
        const event = {timestamp: new Date().toISOString(), event: "QQ_KickedOffLine", ...daodaoKick};
        if (Ge.existsSync(path) && Ge.statSync(path).size >= 1048576) {
          Ge.renameSync(path, path + ".1");
        }
        Ge.appendFileSync(path, JSON.stringify(event) + "\\n", {mode: 384});
      } catch (_) { this.context.logger.logError("DAODAO_KICK_DIAGNOSTIC_WRITE_FAILED"); }
      const i = `[KickedOffLine] [${r.tipsTitle}] ${r.tipsDesc} [QQReasonFields:${JSON.stringify(daodaoKick)}]`;'''
    return source.replace(anchor,replacement)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('destination',type=Path)
    args=parser.parse_args()
    source=args.source.read_text(encoding='utf-8')
    result=patch_source(source)
    with args.destination.open('x',encoding='utf-8') as output:output.write(result)
    print('Prepared only; sha256='+hashlib.sha256(result.encode()).hexdigest())
