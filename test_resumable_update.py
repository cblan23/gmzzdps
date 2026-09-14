import hashlib,io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from resumable_update import download_partial,UpdateDownloadError

class Response:
    def __init__(self,data,status=200,headers=None,fail_after=None):
        self.data=data;self.position=0;self.status=status;self.fail_after=fail_after
        self.headers={'Content-Length':str(len(data)),'Accept-Ranges':'bytes',**(headers or {})}
    def __enter__(self):return self
    def __exit__(self,*_):return False
    def read(self,size):return self.read1(size)
    def read1(self,size):
        if self.fail_after is not None and self.position>=self.fail_after:raise OSError('connection interrupted')
        amount=min(size,4)
        chunk=self.data[self.position:self.position+amount];self.position+=len(chunk);return chunk

class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.destination=Path(self.directory.name)/'client.exe'
        self.payload=b'abcdefgh'
        self.update=SimpleNamespace(size=8,sha256=hashlib.sha256(self.payload).hexdigest())
    def tearDown(self):self.directory.cleanup()
    def test_interrupted_transfer_resumes_at_exact_offset(self):
        offsets=[]
        def opener(offset):
            offsets.append(offset)
            if offset==0:return Response(self.payload,fail_after=4)
            return Response(self.payload[offset:],206,{'Content-Range':'bytes 4-7/8'})
        with patch('resumable_update.time.sleep'):
            partial,_=download_partial(self.update,self.destination,opener)
        self.assertEqual(offsets,[0,4]);self.assertEqual(partial.read_bytes(),self.payload)
    def test_wrong_range_is_rejected_without_appending(self):
        def opener(offset):
            return Response(self.payload,fail_after=4) if not offset else Response(b'xxxx',206,{'Content-Range':'bytes 0-3/8'})
        with patch('resumable_update.time.sleep'),self.assertRaises(UpdateDownloadError):
            download_partial(self.update,self.destination,opener)
        self.assertEqual(self.destination.with_suffix('.exe.download').read_bytes(),b'abcd')
    def test_complete_corruption_does_not_replace_installed_file(self):
        self.destination.write_bytes(b'installed')
        with self.assertRaisesRegex(UpdateDownloadError,'完整更新文件校验失败'):
            download_partial(self.update,self.destination,lambda _:Response(b'xxxxxxxx'))
        self.assertEqual(self.destination.read_bytes(),b'installed')
        self.assertFalse(self.destination.with_suffix('.exe.download').exists())
    def test_maintenance_keeps_partial_and_does_not_retry(self):
        offsets=[]
        def opener(offset):
            offsets.append(offset)
            if not offset:return Response(self.payload,fail_after=4)
            raise HTTPError('http://test',503,'paused',{},io.BytesIO(b'{"error":"update_download_paused"}'))
        with patch('resumable_update.time.sleep'),self.assertRaisesRegex(UpdateDownloadError,'维护'):
            download_partial(self.update,self.destination,opener)
        self.assertEqual(offsets,[0,4]);self.assertEqual(self.destination.with_suffix('.exe.download').read_bytes(),b'abcd')
    def test_changed_release_discards_previous_partial(self):
        self.destination.with_suffix('.exe.download').write_bytes(b'old')
        self.destination.with_suffix('.exe.download.json').write_text(json.dumps({'sha256':'0'*64,'size':8}))
        offsets=[]
        def opener(offset):offsets.append(offset);return Response(self.payload)
        partial,_=download_partial(self.update,self.destination,opener)
        self.assertEqual(offsets,[0]);self.assertEqual(partial.read_bytes(),self.payload)
    def test_legacy_server_without_range_restarts_safely(self):
        calls=[]
        def opener(offset):
            calls.append(offset)
            return Response(self.payload,fail_after=4) if len(calls)==1 else Response(self.payload)
        with patch('resumable_update.time.sleep'):
            partial,_=download_partial(self.update,self.destination,opener)
        self.assertEqual(calls,[0,4]);self.assertEqual(partial.read_bytes(),self.payload)

if __name__=='__main__':unittest.main()
