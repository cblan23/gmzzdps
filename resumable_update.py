"""Bounded update file download with durable partials and final SHA256 checking."""
from __future__ import annotations
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

class UpdateDownloadError(RuntimeError):
    pass

def download_partial(update, destination, open_response, progress=None):
    if not 0 < update.size <= 256 * 1024 * 1024:
        raise UpdateDownloadError('更新文件大小异常')
    if len(update.sha256) != 64 or any(c not in '0123456789abcdefABCDEF' for c in update.sha256):
        raise UpdateDownloadError('更新校验信息无效，请重新检查更新')
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    partial=destination.with_suffix(destination.suffix+'.download')
    metadata=partial.with_suffix(partial.suffix+'.json')
    identity={'sha256':update.sha256.lower(),'size':update.size}
    try:stored=json.loads(metadata.read_text(encoding='utf-8'))
    except (OSError,ValueError):stored=None
    if stored!=identity or (partial.exists() and partial.stat().st_size>update.size):
        partial.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
    metadata.write_text(json.dumps(identity),encoding='utf-8')
    last_error=None
    for attempt in range(2):
        digest=hashlib.sha256()
        written=0
        if partial.exists():
            with partial.open('rb') as previous:
                for block in iter(lambda:previous.read(1024*1024),b''):
                    digest.update(block);written+=len(block)
        if progress:progress(written,update.size)
        supports_resume=False
        try:
            if written<update.size:
                with open_response(written) as response:
                    status=getattr(response,'status',200)
                    etag=response.headers.get('ETag','').strip('"')
                    if len(etag)==64 and all(c in '0123456789abcdefABCDEF' for c in etag) and etag.lower()!=identity['sha256']:
                        raise UpdateDownloadError('服务器更新版本已变化，请重新检查更新')
                    if status==206:
                        expected=f'bytes {written}-{update.size-1}/{update.size}'
                        if response.headers.get('Content-Range')!=expected:
                            raise UpdateDownloadError('服务器续传范围不正确，请稍后重试')
                        supports_resume=True
                    elif status==200:
                        supports_resume=response.headers.get('Accept-Ranges','').lower()=='bytes'
                        if written:
                            written=0;digest=hashlib.sha256()
                            if progress:progress(0,update.size)
                    else:
                        raise UpdateDownloadError('更新服务器返回了无效响应')
                    declared=response.headers.get('Content-Length')
                    if declared is not None and (not declared.isdigit() or int(declared)!=update.size-written):
                        raise UpdateDownloadError('更新文件大小与服务器声明不一致，请重新检查更新')
                    reader=getattr(response,'read1',response.read)
                    with partial.open('ab' if written else 'wb') as output:
                        while written<update.size:
                            block=reader(min(64*1024,update.size-written))
                            if not block:
                                raise OSError('update response ended before complete file')
                            output.write(block);digest.update(block);written+=len(block)
                            if progress:progress(written,update.size)
                        output.flush()
                        os.fsync(output.fileno())
            if digest.hexdigest()!=identity['sha256']:
                partial.unlink(missing_ok=True);metadata.unlink(missing_ok=True)
                raise UpdateDownloadError('完整更新文件校验失败，请重新检查更新后下载')
            return partial,metadata
        except HTTPError as error:
            if error.code==503:
                try:body=json.loads(error.read(4096))
                except (ValueError,OSError):body={}
                if isinstance(body,dict) and body.get('error')=='update_download_paused':
                    raise UpdateDownloadError('更新下载正在维护，请稍后再试；已下载进度已保留') from None
                raise UpdateDownloadError('更新服务暂时繁忙，已保留下载进度，请稍后重试') from None
            raise UpdateDownloadError(f'更新下载返回 HTTP {error.code}，已保留下载进度') from None
        except UpdateDownloadError:
            raise
        except (OSError,URLError,TimeoutError) as error:
            last_error=error
            if not supports_resume or attempt:
                break
            time.sleep(.5)
    raise UpdateDownloadError('更新连接中断，已保留下载进度，重试时将尝试继续下载') from last_error
