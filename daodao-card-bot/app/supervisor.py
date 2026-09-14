import os
import signal
import subprocess
import sys
import time
import random
from app.utils.logging import configure
from app.config import Settings

def main():
    log=configure()
    running=True
    child=None
    def stop(*args):
        nonlocal running
        running=False
        if child and child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    failures=0
    while running:
        try:Settings.read(bot=True)
        except ValueError:
            log.warning('waiting_for_bot_configuration')
            time.sleep(10);continue
        start=time.monotonic()
        # No framework stdout/stderr is retained: it may dump secrets/raw events.
        # Application logs persist through the rotating file handler.
        child=subprocess.Popen([sys.executable,'-m','app.main'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        while running and child.poll() is None:time.sleep(.5)
        if not running:
            try:child.wait(timeout=15)
            except subprocess.TimeoutExpired:child.kill();child.wait()
            break
        failures=0 if time.monotonic()-start>120 else min(failures+1,5)
        delay=random.uniform(.8,1.2)*min(60,2**failures)
        log.warning('websocket_worker_disconnected reconnect_in=%.1f',delay)
        end=time.monotonic()+delay
        while running and time.monotonic()<end:time.sleep(.5)

if __name__=='__main__':main()
