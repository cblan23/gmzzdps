"""Upload immutable public-distribution files to a private OSS bucket via ECS role."""
import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

BUCKET='daodao-dps-updates'
REGION='cn-hangzhou'

def bucket_client():
    import oss2
    from oss2.credentials import Credentials
    from alibabacloud_credentials.client import Client
    from alibabacloud_credentials.models import Config
    class Provider(oss2.CredentialsProvider):
        def __init__(self):self.client=Client(Config(type='ecs_ram_role',role_name='DaodaoOssUploader'))
        def get_credentials(self):
            value=self.client.get_credential()
            return Credentials(value.access_key_id,value.access_key_secret,value.security_token)
    return oss2.Bucket(oss2.ProviderAuthV4(Provider()),'https://oss-cn-hangzhou-internal.aliyuncs.com',BUCKET,region=REGION,connect_timeout=15)

def verify(bucket,key,expected,size):
    response=bucket.get_object(key)
    digest=hashlib.sha256();received=0
    try:
        for chunk in iter(lambda:response.read(1024*1024),b''):
            received+=len(chunk);digest.update(chunk)
    finally:response.close()
    assert received==size and digest.hexdigest()==expected,'Uploaded bytes failed verification'

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('probe','release'))
    parser.add_argument('--directory',type=Path,default=Path('/var/lib/gmzz-dps-monitor'))
    args=parser.parse_args()
    bucket=bucket_client()
    assert bucket.get_bucket_location().location=='oss-cn-hangzhou'
    if args.mode=='probe':
        payload=os.urandom(1024*1024)
        digest=hashlib.sha256(payload).hexdigest();size=len(payload)
        key='diagnostics/'+uuid.uuid4().hex+'/probe.bin'
        bucket.put_object(key,payload,headers={'Content-Type':'application/octet-stream','Cache-Control':'public,max-age=3600','x-oss-forbid-overwrite':'true','x-oss-meta-sha256':digest})
    else:
        root=args.directory.resolve(strict=True)
        metadata=json.loads((root/'update.json').read_text())
        file=root/metadata['filename']
        assert file.parent==root and file.is_file()
        digest=hashlib.sha256(file.read_bytes()).hexdigest();size=file.stat().st_size
        assert digest==metadata['sha256'] and size==metadata['size']
        build=metadata['build_id'];version=metadata['display_version']
        assert len(build)==32 and all(c in '0123456789abcdef' for c in build)
        assert all(c in '0123456789.abcd' for c in version)
        # Keep the immutable legacy object key so already published clients
        # can validate and follow the same CDN update URL after the rename.
        key=f'releases/{build}/Dps-Logs-v{version}.exe'
        if not bucket.object_exists(key):
            branded_name=f'叨叨诡秘助手-v{version}.exe'
            content_disposition=(
                f'attachment; filename="DaodaoMysteryAssistant-v{version}.exe"; '
                f"filename*=UTF-8''{quote(branded_name, safe='')}"
            )
            bucket.put_object_from_file(key,str(file),headers={'Content-Type':'application/octet-stream','Cache-Control':'public,max-age=31536000,immutable',
                'Content-Disposition':content_disposition,'x-oss-forbid-overwrite':'true','x-oss-meta-sha256':digest})
    verify(bucket,key,digest,size)
    print(json.dumps({'uploaded_and_verified':True,'bucket':BUCKET,'key':key,'size':size,'sha256':digest,'cdn_url':'https://downloads.daodaogame.vip/'+key,'permissions':'ECS role; bucket ACL unchanged'}))

if __name__=='__main__':
    try:main()
    except Exception as error:
        # SDK exceptions can contain signed requests. Do not log their repr/body.
        print(json.dumps({'failed':True,'type':type(error).__name__,'status':getattr(error,'status',None),'code':getattr(error,'code',None)}))
        sys.exit(1)
