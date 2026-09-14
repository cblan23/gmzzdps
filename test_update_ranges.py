import hashlib,io,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from server.dps_monitor_server import MonitorHandler

class UpdateRangeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'update.exe';self.path.write_bytes(b'abcdefgh')
        self.metadata={'size':8,'sha256':hashlib.sha256(b'abcdefgh').hexdigest(),'filename':'update.exe','latest_version':'0.2.3'}
        self.handler=object.__new__(MonitorHandler)
        self.handler.headers={};self.handler.wfile=io.BytesIO();self.headers={};self.status=None
        self.handler.send_response=lambda status:setattr(self,'status',status)
        self.handler.send_header=lambda key,value:self.headers.update({key:value})
        self.handler.end_headers=lambda:None
    def tearDown(self):self.directory.cleanup()
    def request(self,headers):
        self.handler.headers=headers
        self.handler._send_update_file(self.metadata,self.path)
        return self.handler.wfile.getvalue()
    def test_resume_returns_exact_range(self):
        body=self.request({'Range':'bytes=4-','If-Range':'"'+self.metadata['sha256']+'"'})
        self.assertEqual(self.status,206);self.assertEqual(body,b'efgh')
        self.assertEqual(self.headers['Content-Range'],'bytes 4-7/8')
        self.assertEqual(self.headers['Content-Length'],'4')
    def test_old_etag_returns_whole_file(self):
        self.assertEqual(self.request({'Range':'bytes=4-','If-Range':'"old"'}),b'abcdefgh')
        self.assertEqual(self.status,200)
    def test_invalid_range_returns_416(self):
        self.assertEqual(self.request({'Range':'bytes=8-'}),b'')
        self.assertEqual(self.status,416);self.assertEqual(self.headers['Content-Range'],'bytes */8')
    def test_legacy_full_download_unchanged(self):
        self.assertEqual(self.request({}),b'abcdefgh');self.assertEqual(self.status,200)
        self.assertEqual(self.headers['Accept-Ranges'],'bytes')
    def test_opt_in_cdn_redirect_is_empty_and_not_cached(self):
        self.metadata.update(legacy_cdn_redirect=True,cdn_download_url='https://downloads.daodaogame.vip/releases/'+'a'*32+'/Dps-Logs-v0.2.3d.exe')
        with patch('server.dps_monitor_server.load_update_metadata',return_value=(self.metadata,self.path)):
            self.handler._download_update()
        self.assertEqual(self.status,302)
        self.assertEqual(self.headers['Location'],self.metadata['cdn_download_url'])
        self.assertEqual(self.headers['Cache-Control'],'no-store')
        self.assertEqual(self.handler.wfile.getvalue(),b'')

if __name__=='__main__':unittest.main()
