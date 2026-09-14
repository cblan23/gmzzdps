import unittest
import io,json
from unittest.mock import Mock,patch
from update_cdn import valid_cdn_url,open_cdn

BUILD='a'*32
URL='https://downloads.daodaogame.vip/releases/'+BUILD+'/Dps-Logs-v0.2.3d.exe'

class CdnTests(unittest.TestCase):
    def test_update_metadata_preserves_cdn_url_and_legacy_path(self):
        from licensing import ServerLicensingGateway,LicensingConnectionError
        gateway=ServerLicensingGateway('http://127.0.0.1:8766','f'*32,'0.2.3')
        payload={'ok':True,'available':True,'latest_version':'0.2.3d','download_path':'/api/v1/dps/update/download',
                 'sha256':'c'*64,'size':10,'filename':'Dps-Logs-v0.2.3d.exe','build_id':BUILD,'cdn_download_url':URL}
        with patch.object(gateway,'_open_get',side_effect=lambda _:io.BytesIO(json.dumps(payload).encode())):
            result=gateway.check_update()
            self.assertEqual(result.cdn_download_url,URL)
            self.assertEqual(result.download_path,'/api/v1/dps/update/download')
            payload['cdn_download_url']='https://untrusted.example/file.exe'
            with self.assertRaises(LicensingConnectionError):gateway.check_update()
    def test_exact_immutable_origin_only(self):
        self.assertTrue(valid_cdn_url(URL,BUILD))
        for url in (URL.replace('https:','http:'),URL.replace('downloads.daodaogame.vip','downloads.daodaogame.vip.attacker.test'),URL+'?token=secret',URL+'#x',URL.replace('/'+BUILD+'/', '/'+('b'*32)+'/'),URL.replace('downloads.','user:password@downloads.'),URL.replace('/Dps-Logs','/../Dps-Logs')):
            with self.subTest(url=url):self.assertFalse(valid_cdn_url(url,BUILD))
    def test_download_sends_no_auth_or_cookies(self):
        opener=Mock()
        with patch('update_cdn.build_opener',return_value=opener):
            open_cdn(URL,BUILD,1024,'c'*64,None)
        request=opener.open.call_args.args[0]
        headers={key.lower():value for key,value in request.header_items()}
        self.assertNotIn('authorization',headers);self.assertNotIn('cookie',headers)
        self.assertEqual(headers['range'],'bytes=1024-')
        self.assertNotIn('if-range',headers)
    def test_external_redirect_is_rejected(self):
        opener=Mock()
        with patch('update_cdn.build_opener',return_value=opener) as build:
            open_cdn(URL,BUILD,0,'c'*64,None)
        redirect=build.call_args.args[1]
        with self.assertRaises(ValueError):redirect.redirect_request(None,None,302,'',{},'https://other.test/file.exe')

if __name__=='__main__':unittest.main()
