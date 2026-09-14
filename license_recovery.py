"""Network outage policy; never extends a signed authorization lease."""
import random
import time

class LicenseRecovery:
    def __init__(self, grace=180.0):
        self.grace = grace
        self.last_success = time.monotonic()
        self.failures = 0
        self.reconnecting = False
        self.successful_renewals = 0

    def failed(self):
        self.reconnecting = True
        self.failures += 1
        return random.uniform(0.8, 1.2) * min(20.0, 2.0 ** min(self.failures, 5))

    def succeeded(self):
        self.last_success = time.monotonic()
        self.failures = 0
        self.reconnecting = False
        self.successful_renewals += 1

    def must_pause(self, session, now=None, wall=None):
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        capability = session.runtime_capability
        lease_expired = capability is not None and capability.expires_at <= wall
        card_expired = session.expires_at is not None and session.expires_at.timestamp() <= wall
        return lease_expired or card_expired or now-self.last_success >= self.grace
