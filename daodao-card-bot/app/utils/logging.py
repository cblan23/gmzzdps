import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

def configure():
    Path('logs').mkdir(exist_ok=True)
    log=logging.getLogger('daodao')
    log.setLevel(logging.INFO)
    if not log.handlers:
        handler=TimedRotatingFileHandler('logs/card-bot.log',when='midnight',backupCount=14,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        log.addHandler(handler)
        console=logging.StreamHandler()
        console.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        log.addHandler(console)
    log.propagate=False
    return log
