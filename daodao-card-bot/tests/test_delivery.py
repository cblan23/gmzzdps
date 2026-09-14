import unittest
from unittest.mock import AsyncMock
from app.bot.delivery import DeliveryApi

class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_uses_explicit_confirmation_and_bounded_timeout(self):
        api=AsyncMock();api.async_callback.return_value={'status':'ok','retcode':0,'data':{'message_id':12}}
        self.assertEqual(await DeliveryApi(api).send_group_msg('999999',[]),'12')
        api.async_callback.assert_awaited_once_with('/send_group_msg',{'group_id':'999999','message':[],'timeout':5000})
    async def test_group_failure_does_not_leak_payload_or_retry(self):
        from app.bot.delivery import GroupDeliveryError
        api=AsyncMock();api.async_callback.return_value={'status':'failed','retcode':1200,'message':'sendMsg SECRET'}
        with self.assertRaises(GroupDeliveryError) as raised:await DeliveryApi(api).send_group_msg('999999',[])
        self.assertEqual(raised.exception.stage,'send_confirmation')
        self.assertNotIn('SECRET',str(raised.exception));self.assertEqual(api.async_callback.await_count,1)
    async def test_group_context_is_private_and_explicit(self):
        api=AsyncMock()
        api.async_callback.return_value={'status':'ok','retcode':0,'data':{'message_id':123}}
        message=[{'type':'text','data':{'text':'private-test'}}]
        self.assertEqual(await DeliveryApi(api).send_private_msg('123456',message,group_id='999999'),'123')
        api.async_callback.assert_awaited_once_with('/send_private_msg',{
            'user_id':'123456','group_id':'999999','message_type':'private','message':message})
        api.send_group_msg.assert_not_called()

    async def test_private_errors_never_fallback_to_group(self):
        for result in ({'status':'failed','retcode':1200,'data':None},{'status':'ok','retcode':0,'data':{}}):
            api=AsyncMock();api.async_callback.return_value=result
            with self.assertRaises(RuntimeError):
                await DeliveryApi(api).send_private_msg('123456',[],group_id='999999')
            api.send_group_msg.assert_not_called()
