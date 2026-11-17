"""Use public DNS and IP-pinned TLS even when the local proxy supplies fake DNS."""
import ipaddress,json,socket,urllib.parse,urllib.request
from assets import AssetTransport,PinnedHTTPS
from core import Hold,NoRedirect

class ProxyPinnedHTTPS(PinnedHTTPS):
    def __init__(self,host,address,timeout,proxy):
        super().__init__(host,address,timeout)
        self.proxy=proxy
    def connect(self):
        self.sock=socket.create_connection(self.proxy,timeout=self.timeout)
        try:
            self._tunnel_host,self._tunnel_port=self.address,443
            self._tunnel()
            self.sock=self._context.wrap_socket(self.sock,server_hostname=self.host)
        except BaseException:
            self.close();raise

class PublicDNS:
    def __init__(self):self.cache={}
    def __call__(self,host,port,**_):
        if host not in self.cache:
            url='https://dns.google/resolve?'+urllib.parse.urlencode({'name':host,'type':'A'})
            opener=urllib.request.build_opener(NoRedirect())
            with opener.open(urllib.request.Request(url,headers={'Accept':'application/dns-json'}),timeout=20) as response:
                raw=response.read(65537)
            if len(raw)>65536:raise Hold('public_dns_response_limit')
            data=json.loads(raw)
            if data.get('Status')!=0:raise Hold('public_dns_failed')
            addresses=[a.get('data') for a in data.get('Answer',[]) if a.get('type')==1]
            if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):raise Hold('public_dns_not_public')
            self.cache[host]=sorted(set(addresses))
        return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',(a,port)) for a in self.cache[host]]

def media_transport(allowed_hosts,*,use_local_proxy=False):
    if not use_local_proxy:return AssetTransport(allowed_hosts)
    proxy_url=urllib.request.getproxies().get('https')
    parsed=urllib.parse.urlsplit(proxy_url or '')
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.username or parsed.password or not parsed.port:
        raise Hold('trusted_local_proxy_unavailable')
    proxy=(parsed.hostname,parsed.port)
    def connection(host,address,timeout):return ProxyPinnedHTTPS(host,address,timeout,proxy)
    return AssetTransport(allowed_hosts,resolver=PublicDNS(),connection_factory=connection)
