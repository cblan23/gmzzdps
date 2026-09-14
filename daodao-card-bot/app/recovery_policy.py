"""Recovery decisions independent of QQ APIs and systemd side effects."""
import json
import os
from pathlib import Path


class RecoveryPolicy:
    FAILURE_THRESHOLD = 3
    COOLDOWN = 30*60

    def __init__(self, path=Path('data/qq-recovery-budget.json')):
        self.path = Path(path)
        self.failures = 0
        self.last_attempt = 0
        self.storage_error = False
        try:
            value=json.loads(self.path.read_text(encoding='utf-8'))
            timestamp=value['last_attempt']
            if type(timestamp) not in (int,float) or not 0<=timestamp<1e12:raise ValueError()
            self.last_attempt=timestamp
        except FileNotFoundError:pass
        except (OSError,ValueError,KeyError,TypeError):self.storage_error=True

    def record_probe(self, success):
        self.failures = 0 if success else min(self.failures+1, self.FAILURE_THRESHOLD)

    def eligible(self, now, online, busy):
        return (online is True and not busy and not self.storage_error
                and self.failures>=self.FAILURE_THRESHOLD
                and now-self.last_attempt>=self.COOLDOWN)

    def reserve(self, now):
        """Persist before mutation. Failed persistence disables automatic restart."""
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.path.with_suffix('.tmp')
            with temporary.open('w',encoding='utf-8') as stream:
                os.chmod(temporary,0o600)
                json.dump({'last_attempt':now},stream)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.path)
            self.last_attempt=now
            return True
        except OSError:
            self.storage_error=True
            return False


def health_state(online, worker, probe, operation, now):
    """Interface readiness is not an assertion of end-user message delivery."""
    state=(operation or {}).get('state')
    if state=='running':return 'recovering'
    if state=='awaiting_scan':return 'awaiting_scan'
    if online is False:return 'login_required'
    if online is not True:return 'connection_unknown'
    if not probe.get('checked_at') or now-probe['checked_at']>100:return 'checking'
    if probe.get('ok') is not True:return 'qq_unresponsive'
    if not worker:return 'worker_disconnected'
    return 'interfaces_ready'
