"""Credential-free downloads from the single configured CDN origin."""
import re
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, HTTPSHandler, build_opener

def valid_cdn_url(url,build_id):
    if not isinstance(url,str) or not re.fullmatch(r'[a-f0-9]{32}',build_id):return False
    try:parts=urlsplit(url)
    except ValueError:return False
    return (parts.scheme=='https' and parts.netloc=='downloads.daodaogame.vip'
            and not parts.query and not parts.fragment
            and bool(re.fullmatch('/releases/'+build_id+r'/Dps-Logs-v[0-9]+(?:\.[0-9]+){2,3}[a-z]?\.exe',parts.path)))

def open_cdn(url,build_id,offset,digest,context,timeout=30):
    if not valid_cdn_url(url,build_id):raise ValueError('Untrusted update CDN URL')
    class SameOriginRedirect(HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            if not valid_cdn_url(newurl,build_id):raise ValueError('Untrusted CDN redirect')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    headers={'Accept-Encoding':'identity','User-Agent':'Dps-Logs-Updater'}
    # OSS ETag is not SHA256. This URL is immutable and pinned to build_id;
    # final SHA256 remains authoritative. Sending SHA256 as If-Range would
    # mismatch OSS's ETag and silently restart every partial transfer.
    if offset:headers['Range']=f'bytes={offset}-'
    return build_opener(HTTPSHandler(context=context),SameOriginRedirect()).open(Request(url,headers=headers),timeout=timeout)
