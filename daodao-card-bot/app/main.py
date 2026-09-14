"""NcatBot connect-only worker, restarted by a bounded-backoff supervisor."""
import asyncio
import logging
import os
import sys
import json
import time
from pathlib import Path
from app.config import Settings
from app.utils.logging import configure
from app.utils.diagnostics import emit

def main():
    os.umask(0o077)
    settings=Settings.read(bot=True)
    # NcatBot logs event bodies/configuration. Drop all non-application logs
    # before importing it, rather than attempting to redact arbitrary cards.
    original=logging.Logger.handle
    def safe_handle(logger,record):
        if record.name.startswith('daodao'):return original(logger,record)
    logging.Logger.handle=safe_handle
    configure()
    from ncatbot.core import BotClient
    from ncatbot.utils import ncatbot_config
    from ncatbot.core.adapter.nc import launch
    from app.bot.handlers import BotHandler
    from app.bot.delivery import DeliveryApi
    from app.services.remote_card_service import RemoteCardApiService
    from app.services.local_card_service import LocalCardService
    cfg=ncatbot_config
    cfg.bt_uin=settings.bot_qq
    cfg.root=sorted(settings.admins)[0]
    cfg.napcat.ws_uri=settings.ws_url
    cfg.napcat.ws_token=settings.ws_token
    cfg.napcat.remote_mode=True
    cfg.napcat.enable_webui=False
    cfg.enable_webui_interaction=False
    cfg.check_ncatbot_update=False
    cfg.skip_ncatbot_install_check=True
    cfg.debug=False
    cfg.plugin.plugins_dir='data/plugins'
    cfg.plugin.skip_plugin_load=True
    # Keep credentials in environment, never persist NcatBot's generated YAML.
    cfg.save=lambda *a,**kw:None
    original_probe=launch.test_websocket
    async def bounded_probe(*args,**kwargs):
        try:return await asyncio.wait_for(original_probe(*args,**kwargs),10)
        except asyncio.TimeoutError:return False
    launch.test_websocket=bounded_probe
    bot=BotClient()
    async def bounded_api(path,params=None):
        return await bot.adapter.send(path,params,timeout=10)
    bot.api.async_callback=bounded_api
    service=RemoteCardApiService(settings.api_url,settings.api_token) if settings.mode=='remote' else LocalCardService(settings.db)
    handler=BotHandler(settings,service,DeliveryApi(bot.api))
    bot.on_group_message()(handler.handle)
    log=logging.getLogger('daodao.bot')
    def record_connection(online):
        path=Path('data/bot-connection.json')
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'pid':os.getpid(),'at':time.time(),'online':online}),encoding='utf-8')
        os.replace(temporary,path)
    @bot.on_startup()
    async def startup(event):
        if str(event.self_id)!=settings.bot_qq:
            log.error('wrong_bot_account');os._exit(2)
        log.info('napcat_connected qq=%s',settings.bot_qq)
        emit('worker','onebot_connected',qq=settings.bot_qq)
        record_connection(True)
    @bot.on_heartbeat()
    async def heartbeat(event):
        status=getattr(event,'status',None)
        online=status.get('online') if isinstance(status,dict) else getattr(status,'online',None)
        record_connection(online is True)
        emit('worker','heartbeat',online=online)
        if online is False:log.warning('qq_offline_scan_may_be_required')
    log.info('bot_starting mode=connect qq=%s',settings.bot_qq)
    emit('worker','worker_starting')
    bot.run_frontend()

if __name__=='__main__':
    try:main()
    except Exception as error:
        # Third-party exception text may contain WS token or message payload.
        configure().error('worker_stopped type=%s',type(error).__name__)
        sys.exit(1)
