"""Local, isolated Mihomo node measurements. Never modifies Clash Verge.

Run:  python node_lab.py [port]          start the local web UI (default 8775)
      python node_lab.py --ensure-core   find or download a Mihomo core, then exit
"""
import os, sys, json, time, socket, secrets, subprocess, threading, statistics, hashlib
import datetime, pathlib, urllib.request, urllib.parse, urllib.error, queue, re, shutil, zipfile, io, platform
import atexit
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

APP = pathlib.Path(__file__).resolve().parent
import yaml
from unlock_checks import fetch, check_all
from report_export import export_report
from node_groups import classify, group_nodes, representatives
import speedtest

TZ = datetime.timezone(datetime.timedelta(hours=8))
CLASH = pathlib.Path(os.environ.get('CLASH_VERGE_DIR') or (pathlib.Path(os.environ.get('APPDATA', '')) / 'io.github.clash-verge-rev.clash-verge-rev'))
RUNTIME = APP / 'runtime'
SUBS_DIR = RUNTIME / 'subscriptions'
RESULTS = APP.parent / '测评结果'
STATE = {'running': False, 'phase': '等待开始', 'completed': 0, 'total': 0, 'rows': [], 'files': {}, 'error': ''}
LOCK = threading.RLock()
CANCEL = threading.Event()
TOKEN = secrets.token_urlsafe(24)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
GEO_CACHE = {}
ACTIVE_CORES = []
URL_SUBS = {}          # id -> {'name', 'file'}; the URL itself is never written to results
DELAY_URLS = ('https://www.gstatic.com/generate_204', 'https://cp.cloudflare.com/generate_204')


def now():
    return datetime.datetime.now(TZ).isoformat(timespec='seconds')


def load_yaml(path):
    return yaml.safe_load(pathlib.Path(path).read_text(encoding='utf-8-sig')) or {}


# --------------------------------------------------------------------------- mihomo core

def core_candidates():
    exe = '.exe' if os.name == 'nt' else ''
    env = os.environ.get('MIHOMO_PATH')
    if env:
        yield pathlib.Path(env)
    yield APP / 'bin' / f'mihomo{exe}'
    roots = [os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('ProgramFiles(x86)', ''),
             str(pathlib.Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs')]
    for root in filter(None, roots):
        for folder in ('Clash Verge', 'clash-verge', 'Clash Verge Rev'):
            for name in ('verge-mihomo.exe', 'verge-mihomo-alpha.exe'):
                yield pathlib.Path(root) / folder / name
    for name in ('mihomo', 'verge-mihomo', 'clash-meta'):
        found = shutil.which(name)
        if found:
            yield pathlib.Path(found)


def find_core():
    return next((p for p in core_candidates() if p.is_file()), None)


def local_clash_proxy():
    """Mixed port of the running Clash Verge, used only as a download fallback."""
    for name in ('clash-verge.yaml', 'config.yaml', 'verge.yaml'):
        try:
            cfg = load_yaml(CLASH / name)
        except Exception:
            continue
        port = cfg.get('mixed-port') or cfg.get('verge_mixed_port') or cfg.get('port')
        if port:
            return f'http://127.0.0.1:{port}'
    return 'http://127.0.0.1:7897'


def http_get(url, timeout=20, headers=None, limit=60_000_000):
    """GET directly, then through the local Clash port if direct access fails."""
    last = None
    for proxy in (None, local_clash_proxy()):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}))
        req = urllib.request.Request(url, headers={'User-Agent': 'clash-verge/v2.2.3 mihomo', **(headers or {})})
        try:
            with opener.open(req, timeout=timeout) as r:
                body = r.read(limit + 1)
                if len(body) > limit:
                    raise ValueError('响应过大')
                return body, dict(r.headers)
        except Exception as exc:
            last = exc
    raise last


def ensure_core():
    """Use Clash Verge's core if present; otherwise download Mihomo from GitHub into app/bin."""
    core = find_core()
    if core:
        print(f'Mihomo 内核：{core}', flush=True)
        return core
    if os.name != 'nt':
        raise RuntimeError('未找到 mihomo，可设置环境变量 MIHOMO_PATH 指向内核。')
    arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'amd64-compatible'
    print('未找到 Clash Verge 内核，正在从 GitHub 下载 Mihomo …', flush=True)
    info = json.loads(http_get('https://api.github.com/repos/MetaCubeX/mihomo/releases/latest', headers={'Accept': 'application/vnd.github+json'})[0])
    asset = next(a for a in info['assets'] if re.fullmatch(rf'mihomo-windows-{arch}-v[\d.]+\.zip', a['name']))
    data, _ = http_get(asset['browser_download_url'], timeout=120)
    target = APP / 'bin' / 'mihomo.exe'
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = next(n for n in z.namelist() if n.lower().endswith('.exe'))
        target.write_bytes(z.read(member))
    print(f'已下载 {asset["name"]} → {target}', flush=True)
    return target


# --------------------------------------------------------------------------- subscriptions

def profiles():
    result = [{'id': k, 'name': v['name'] + '（链接）', 'current': False} for k, v in URL_SUBS.items()]
    try:
        p = load_yaml(CLASH / 'profiles.yaml')
    except Exception:
        return result
    for item in p.get('items', []):
        if item.get('type') not in ('remote', 'local'):
            continue
        file = CLASH / 'profiles' / item.get('file', '')
        if file.is_file():
            result.append({'id': item['uid'], 'name': item.get('name') or item['uid'], 'current': item['uid'] == p.get('current')})
    return result


def add_url_subscription(url):
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError('请输入 http(s) 开头的订阅链接。')
    body, headers = http_get(url, timeout=25, limit=20_000_000)
    if not body.strip():
        raise ValueError('订阅返回为空。')
    uid = 'url-' + hashlib.sha256(url.encode()).hexdigest()[:12]
    SUBS_DIR.mkdir(parents=True, exist_ok=True)
    file = SUBS_DIR / f'{uid}.txt'
    file.write_bytes(body)
    name = ''
    disposition = {k.lower(): v for k, v in headers.items()}.get('content-disposition', '')
    m = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I) or re.search(r'filename="?([^";]+)', disposition, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1))
    name = re.sub(r'\.ya?ml$', '', name, flags=re.I).strip() or parts.hostname
    URL_SUBS[uid] = {'name': name, 'file': file}
    return uid, name


def subscription(uid):
    """Return (name, config, statically-known nodes) for a Clash Verge profile or a pasted link."""
    if uid in URL_SUBS:
        entry = URL_SUBS[uid]
        raw = entry['file'].read_text(encoding='utf-8-sig', errors='replace')
        try:
            cfg = yaml.safe_load(raw)
        except Exception:
            cfg = None
        if isinstance(cfg, dict) and isinstance(cfg.get('proxies'), list):
            nodes = [x for x in cfg['proxies'] if isinstance(x, dict) and x.get('name')]
            return entry['name'], cfg, nodes
        # Base64 / share-link subscriptions: let Mihomo parse them as a file provider.
        return entry['name'], {'proxies': [], 'proxy-providers': {'link': {'type': 'file', 'path': str(entry['file'])}}}, []
    p = load_yaml(CLASH / 'profiles.yaml')
    entry = next((x for x in p.get('items', []) if x.get('uid') == uid and x.get('type') in ('remote', 'local')), None)
    if not entry:
        raise ValueError('找不到所选订阅，请刷新页面。')
    source = (CLASH / 'profiles' / entry['file']).resolve()
    if not source.is_relative_to((CLASH / 'profiles').resolve()):
        raise ValueError('无效配置路径')
    cfg = load_yaml(source)
    nodes = [x for x in cfg.get('proxies', []) if isinstance(x, dict) and x.get('name')]
    return entry.get('name') or uid, cfg, nodes


def physical_node(p):
    return not re.search(r'剩余流量|套餐到期|到期时间|最新官网|邮箱客服|欢迎加入|Telegram群组|支持AI:|官网地址|订阅更新|重要提示|永久--|国内-china|距离下次|重置剩余|建议.*订阅|t[.]me[/ .]|电报.*t[.]me|剩余.*天|流量重置', p.get('name', ''), re.I)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class IsolatedCore:
    def __init__(self, config, workers, residential):
        self.core = find_core()
        if not self.core:
            raise RuntimeError('未找到 Mihomo 内核。请安装 Clash Verge Rev，或重新运行启动入口自动下载。')
        self.root = RUNTIME / secrets.token_hex(8)
        self.root.mkdir(parents=True)
        self.controller = free_port()
        self.secret = secrets.token_hex(16)
        self.ports = [free_port() for _ in range(workers)]
        self.proc = None
        self.log = None
        cfg = {'mode': 'rule', 'ipv6': False, 'allow-lan': False, 'log-level': 'warning',
               'unified-delay': True,
               'external-controller': f'127.0.0.1:{self.controller}', 'secret': self.secret,
               'find-process-mode': 'off', 'rules': ['MATCH,REJECT'], 'tun': {'enable': False},
               'dns': {'enable': True, 'ipv6': False, 'enhanced-mode': 'redir-host',
                       'default-nameserver': ['223.5.5.5', '119.29.29.29'],
                       'nameserver': ['https://dns.alidns.com/dns-query', 'https://doh.pub/dns-query'],
                       'proxy-server-nameserver': ['https://dns.alidns.com/dns-query']},
               'proxies': list(config.get('proxies', [])),
               'proxy-groups': json.loads(json.dumps(config.get('proxy-groups', []) or [])),
               'proxy-providers': json.loads(json.dumps(config.get('proxy-providers', {}) or {}))}
        for i, provider in enumerate(cfg['proxy-providers'].values()):
            original = provider.get('path')
            provider['path'] = str(self.root / f'provider-{i}.yaml')
            if provider.get('type') == 'file' and original:
                src = pathlib.Path(original)
                if not src.is_absolute():
                    src = CLASH / src
                if not src.exists():
                    src = CLASH / 'profiles' / original
                if not src.exists():
                    raise ValueError('订阅 provider 本地文件不存在')
                shutil.copy2(src, provider['path'])
            provider['health-check'] = {'enable': False}
        # Keep the subscription's own groups (a node may chain through one via dialer-proxy),
        # but turn auto groups into plain selectors so nothing health-checks in the background.
        for g in cfg['proxy-groups']:
            if g.get('type') in ('url-test', 'fallback', 'load-balance'):
                g['type'] = 'select'
            for k in ('url', 'interval', 'tolerance', 'lazy', 'strategy', 'expected-status'):
                g.pop(k, None)
        names = [p['name'] for p in cfg['proxies']]
        providers = list(cfg['proxy-providers'])
        cfg['listeners'] = []
        for i, port in enumerate(self.ports):
            select = {'name': f'Lab{i}-Node', 'type': 'select', 'proxies': names or ['DIRECT']}
            if providers:
                select['use'] = providers
                if not names:
                    select.pop('proxies')
            cfg['proxy-groups'].append(select)
            entry = [select['name']]
            if residential:
                home = dict(residential)
                home['name'] = f'Lab{i}-Home'
                home['dialer-proxy'] = select['name']
                cfg['proxies'].append(home)
                entry.append(home['name'])
            cfg['proxy-groups'].append({'name': f'Lab{i}-Input', 'type': 'select', 'proxies': entry})
            cfg['listeners'].append({'name': f'lab-{i}', 'type': 'mixed', 'listen': '127.0.0.1', 'port': port, 'proxy': f'Lab{i}-Input'})
        self.config = self.root / 'config.yaml'
        self.config.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding='utf-8')

    def start(self):
        ACTIVE_CORES.append(self)
        env = {k: v for k, v in os.environ.items() if k.upper() not in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')}
        self.log = open(self.root / 'core.log', 'w', encoding='utf-8')
        self.proc = subprocess.Popen([str(self.core), '-d', str(self.root), '-f', str(self.config)], stdout=self.log, stderr=self.log, env=env, creationflags=FLAGS)
        for _ in range(160):
            if self.proc.poll() is not None:
                tail = (self.root / 'core.log').read_text(encoding='utf-8', errors='replace')[-300:]
                raise RuntimeError('独立测试内核启动失败；请检查订阅协议与 provider。' + tail)
            try:
                self.api('GET', '/version')
                return
            except Exception:
                time.sleep(.15)
        raise RuntimeError('独立测试内核启动超时')

    def api(self, method, path, payload=None, timeout=12):
        b = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(f'http://127.0.0.1:{self.controller}{path}', data=b, method=method,
                                   headers={'Authorization': f'Bearer {self.secret}', 'Content-Type': 'application/json'})
        with OPENER.open(r, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}

    def nodes(self):
        return self.api('GET', '/proxies/' + urllib.parse.quote('Lab0-Node', safe='')).get('all', [])

    def choose(self, worker, name, chain):
        self.api('PUT', '/proxies/' + urllib.parse.quote(f'Lab{worker}-Node', safe=''), {'name': name})
        self.api('PUT', '/proxies/' + urllib.parse.quote(f'Lab{worker}-Input', safe=''), {'name': f'Lab{worker}-Home' if chain else f'Lab{worker}-Node'})

    def delay(self, worker, url, timeout_ms=5000):
        """Mihomo's own delay test (unified-delay: handshake excluded, ≈1 RTT through the node)."""
        q = urllib.parse.urlencode({'url': url, 'timeout': timeout_ms})
        try:
            return self.api('GET', f'/proxies/{urllib.parse.quote(f"Lab{worker}-Input", safe="")}/delay?{q}', timeout=timeout_ms / 1000 + 3).get('delay')
        except Exception:
            return None

    def close(self):
        if self in ACTIVE_CORES:
            ACTIVE_CORES.remove(self)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.log:
            self.log.close()
        if self.root.resolve().is_relative_to(RUNTIME.resolve()):
            shutil.rmtree(self.root, ignore_errors=True)


def resolve_nodes(core, nodes):
    """All selectable nodes, including ones that only exist inside providers."""
    all_proxies = core.api('GET', '/proxies').get('proxies', {})
    node_map = {n['name']: n for n in nodes}
    try:
        for provider in core.api('GET', '/providers/proxies').get('providers', {}).values():
            for p in provider.get('proxies', []) or []:
                node_map.setdefault(p['name'], {'name': p['name'], 'type': p.get('type', '')})
    except Exception:
        pass
    for provider_file in core.root.glob('provider-*.yaml'):
        try:
            for p in load_yaml(provider_file).get('proxies', []) or []:
                node_map.setdefault(p['name'], p)
        except Exception:
            pass   # base64 provider files are not YAML; the API below still lists them
    result = []
    for name in core.nodes():
        n = node_map.get(name, {'name': name, 'type': all_proxies.get(name, {}).get('type', 'provider')})
        n = dict(n)
        n['type'] = str(n.get('type') or all_proxies.get(name, {}).get('type', '')).lower()
        if name in ('DIRECT', 'REJECT') or not physical_node(n):
            continue
        result.append(n)
    return result


def list_nodes(uid):
    _, cfg, nodes = subscription(uid)
    if not cfg.get('proxy-providers'):
        return [p for p in nodes if physical_node(p)], False
    core = IsolatedCore(cfg, 1, None)
    try:
        core.start()
        return resolve_nodes(core, nodes), True
    finally:
        core.close()


def residence():
    try:
        cfg = load_yaml(CLASH / 'clash-verge.yaml')
    except Exception:
        return None
    return next((dict(p) for p in cfg.get('proxies', []) if p.get('type') == 'socks5' and ('住宅' in p.get('name', ''))), None)


def geo(ip, proxy):
    if not ip:
        return {}
    with LOCK:
        if ip in GEO_CACHE:
            return GEO_CACHE[ip]
    r = fetch('https://ipwho.is/' + urllib.parse.quote(ip, safe=''), proxy, timeout=6)
    try:
        obj = json.loads(r['body'])
        if obj.get('success') is False:
            return {}
        value = {'country': obj.get('country_code', ''), 'asn': str(obj.get('connection', {}).get('asn', '')), 'org': obj.get('connection', {}).get('org', '')}
    except Exception:
        return {}
    with LOCK:
        GEO_CACHE[ip] = value
    return value


def endpoint_info(node, proxy):
    entry_ip = ''
    try:
        entry_ip = socket.getaddrinfo(node.get('server', ''), None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except Exception:
        pass
    trace = fetch('https://www.cloudflare.com/cdn-cgi/trace', proxy, timeout=8)
    fields = dict(line.split('=', 1) for line in trace.get('body', '').splitlines() if '=' in line)
    exit_ip, country = fields.get('ip', ''), fields.get('loc', '')
    eg = geo(entry_ip, proxy)
    xg = geo(exit_ip, proxy)
    return {'entry_ip': entry_ip, 'entry_country': eg.get('country', ''), 'entry_asn': eg.get('asn', ''),
            'exit_ip': exit_ip, 'exit_country': country or xg.get('country', ''), 'exit_asn': xg.get('asn', '')}


def node_country(node, row=None):
    c = (row or {}).get('exit_country') or classify(node).get('country', '')
    return '' if c == '未知' else c


def live_reporter(key, label, kind, target_mb):
    def report(info):
        with LOCK:
            STATE.setdefault('downloads', {})[key] = {
                'node': label, 'kind': kind, 'source': info['source'], 'target_mb': target_mb,
                'download_mb': round(info['done_bytes'] / 1e6, 2), 'elapsed_s': round(info['elapsed_s'], 1),
                'current_mbps': round(info['current_mbps'], 2), 'average_mbps': round(info['average_mbps'], 2),
                'percent': round(min(100, info['done_bytes'] * 100 / max(info['target_bytes'], 1)), 1)}
    return report


def clear_live(key):
    with LOCK:
        STATE.setdefault('downloads', {}).pop(key, None)


def measure(core, worker, node, chain, speed_mb):
    core.choose(worker, node['name'], chain)
    samples = []
    for _ in range(3):
        if CANCEL.is_set():
            break
        d = core.delay(worker, DELAY_URLS[0])
        if d:
            samples.append(float(d))
    primary_ok = len(samples)
    if not samples and not CANCEL.is_set():
        # A node may only be blocked from Google; one try against Cloudflare before declaring it dead.
        d = core.delay(worker, DELAY_URLS[1])
        if d:
            samples.append(float(d))
    row = {'node': node['name'], 'chain': '住宅链路' if chain else '订阅节点', 'protocol': node.get('type', 'provider'),
           'latency_ms': round(statistics.median(samples), 1) if samples else None,
           'jitter_ms': round(max(samples) - min(samples), 1) if len(samples) > 1 else None,
           'loss_pct': round((3 - primary_ok) * 100 / 3, 1),
           'speed_mbps': None, 'upload_mbps': None, 'download_mb': 0,
           'connectivity': '可达' if samples else '延迟探测失败', 'checks': {}, 'error': ''}
    row['latency_samples_ms'] = samples
    row['latency_successes'] = len(samples)
    row['latency_basis'] = 'Mihomo unified-delay（不含握手，≈经节点到 Google 的 1 个往返）'
    row['stage'] = '延迟通过' if samples else '延迟全部失败，跳过后续'
    if not samples:
        row['error'] = row['stage']
    row['measured_at'] = now()
    group = node.get('_group') or {**classify(node), 'members': [node['name']]}
    row['group_label'] = group['group_label']
    row['group_members'] = group['members']
    row['group_size'] = len(group['members'])
    row['speed_requested_mb'] = speed_mb
    return row


def update(**kwargs):
    with LOCK:
        STATE.update(kwargs)


def run_test(options):
    core = None
    output = RESULTS / datetime.datetime.now(TZ).strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True, exist_ok=True)
    base_rows = options.get('_base_rows', [])
    speed_mb = int(options.get('speed_mb', 0))
    upload_mb = int(options.get('upload_mb', 0))
    streams = int(options.get('streams', 4))
    data = {'meta': {**options.get('_base_meta', {}), 'started': now(), 'test_source': '本机', 'mode': options.get('mode', 'direct'),
                     'speed_mb': speed_mb, 'upload_mb': upload_mb, 'streams': streams, 'dedupe': bool(options.get('dedupe', True))},
            'rows': list(base_rows)}
    if base_rows:
        data['meta']['note'] = '单节点重测后合并结果；每行以 measured_at 为实际测量时间。'
    try:
        name, cfg, nodes = subscription(options['profile'])
        data['meta']['profile'] = name
        data['meta']['profile_id'] = options['profile']
        workers = int(options.get('concurrency', 16))
        mode = options.get('mode', 'direct')
        home = residence() if mode in ('chain', 'both') else None
        if mode in ('chain', 'both') and not home:
            raise ValueError('当前 Clash Verge 配置中没有名称含“住宅”的 SOCKS5 节点。')
        core = IsolatedCore(cfg, workers + 4, home)
        queue_lanes = queue.Queue()
        for lane in range(workers, workers + 4):
            queue_lanes.put(lane)
        update(phase='正在启动独立测试内核')
        core.start()
        wanted = set(options.get('nodes') or [])
        selected = [n for n in resolve_nodes(core, nodes) if not wanted or n['name'] in wanted]
        filter_text = options.get('filter', '').strip()
        if filter_text:
            selected = [n for n in selected if filter_text.casefold() in n['name'].casefold()]
        if not selected:
            raise ValueError('没有匹配的可测试节点。')
        data['meta']['before_group_count'] = len(selected)
        if options.get('dedupe', True):
            selected = representatives(selected)
        jobs = [(n, False) for n in selected] if mode in ('direct', 'both') else []
        if mode in ('chain', 'both'):
            jobs += [(n, True) for n in selected]
        data['meta']['node_count'] = len(selected)
        data['meta'].update(latency_samples=3, latency_method='mihomo unified-delay', small_download_mb=1, screening_concurrency=workers,
                            planned_traffic_mb=len(jobs) * (1 + speed_mb + upload_mb))
        per_node = (speed_mb * 8 / 16 + 12 if speed_mb else 0) + (max(10, upload_mb) + 12 if upload_mb else 0)
        update(eta_upper_s=round(20 + ((len(jobs) + workers - 1) // workers) * 25 + len(jobs) * per_node + ((len(jobs) + 3) // 4) * 40),
               run_started_epoch=time.time())
        measured = {}

        def publish(n, chain, r):
            key = (n['name'], chain)
            with LOCK:
                old = next((x for x in data['rows'] if x['node'] == n['name'] and x['chain'] == ('住宅链路' if chain else '订阅节点')), None)
                if old and base_rows:
                    for k in ('group_label', 'group_members', 'group_size'):
                        r[k] = old.get(k, r.get(k))
                    if key not in measured:
                        r['previous_test_traffic_bytes'] = old.get('previous_test_traffic_bytes', 0) + old.get('traffic_download_bytes', 0) + old.get('traffic_upload_bytes', 0)
                    if options.get('speed_only'):
                        for k in ('checks', 'entry_ip', 'entry_country', 'entry_asn', 'exit_ip', 'exit_country', 'exit_asn'):
                            r[k] = old.get(k, r.get(k))
                        r['checks_measured_at'] = old.get('checks_measured_at', old.get('measured_at'))
                data['rows'] = [x for x in data['rows'] if not (x['node'] == r['node'] and x['chain'] == r['chain'])]
                data['rows'].append(r)
                measured[key] = r
                STATE['rows'] = list(data['rows'])
                (output / '原始结果.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

        def phase(title, phase_jobs, concurrency, action):
            update(phase=title, completed=0, total=len(phase_jobs))
            q = queue.Queue()
            for job in phase_jobs:
                q.put(job)
            done = 0

            def worker(i):
                nonlocal done
                while not CANCEL.is_set():
                    try:
                        n, chain = q.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        core.choose(i, n['name'], chain)
                        r = action(i, n, chain, f'http://127.0.0.1:{core.ports[i]}')
                    except Exception as exc:
                        r = dict(measured.get((n['name'], chain), {'node': n['name'], 'chain': '住宅链路' if chain else '订阅节点', 'checks': {}}))
                        r.update(error=f'{type(exc).__name__}: {exc}'[:200], stage=title + '失败')
                    publish(n, chain, r)
                    with LOCK:
                        done += 1
                        STATE['completed'] = done
                        STATE['phase'] = f'{title} {done}/{len(phase_jobs)}：' + n['name']
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                for f in as_completed([pool.submit(worker, i) for i in range(concurrency)]):
                    f.result()

        phase('阶段1：延迟（Mihomo，3次）', jobs, workers, lambda i, n, c, p: measure(core, i, n, c, speed_mb))
        alive = [job for job in jobs if measured.get((job[0]['name'], job[1]), {}).get('latency_successes', 0) >= 1]

        def small(i, n, c, proxy):
            r = dict(measured[(n['name'], c)])
            res = speedtest.download_test(proxy, 1, 1, node_country(n), CANCEL, transfer_limit=8)
            r['traffic_download_bytes'] = r.get('traffic_download_bytes', 0) + res.get('traffic_download_bytes', 0)
            ok = res.get('speed_mbps') is not None
            r.update(small_speed_mbps=res.get('speed_avg_mbps'), small_download_mb=round(r.get('small_download_mb', 0) + res.get('download_mb', 0), 2),
                     small_error=res.get('error', ''), small_attempts=r.get('small_attempts', 0) + 1,
                     error='' if ok else '1MB连通测试失败：' + res.get('error', ''),
                     stage='连通测试通过' if ok else '1MB连通测试失败，跳过测速')
            return r
        if alive and not CANCEL.is_set():
            phase('阶段2：1MB连通测试（只判断能否下载，不作测速）', alive, workers, small)
        retry = [job for job in alive if measured.get((job[0]['name'], job[1]), {}).get('small_speed_mbps') is None]
        if retry and not CANCEL.is_set():
            phase('阶段2补测：单线程重试', retry, 1, small)
        passed = [job for job in alive if measured.get((job[0]['name'], job[1]), {}).get('small_speed_mbps') is not None]

        # Platform checks finish before the speed phase so they never share bandwidth with it.
        if passed and not CANCEL.is_set() and not options.get('speed_only'):
            lanes = list(range(workers, workers + 4))
            update(phase='阶段3：AI / 流媒体检测', completed=0, total=len(passed))

            def inspect(job, lane):
                n, c = job
                core.choose(lane, n['name'], c)
                proxy = f'http://127.0.0.1:{core.ports[lane]}'
                info = endpoint_info(n, proxy) if options.get('endpoint_lookup') is True else {'endpoint_lookup_status': '未启用'}
                info['checks'] = check_all(proxy)
                info['checks_measured_at'] = now()
                return info
            pending = list(passed)
            with ThreadPoolExecutor(max_workers=4) as pool:
                while pending and not CANCEL.is_set():
                    batch, pending = pending[:4], pending[4:]
                    futures = {pool.submit(inspect, job, lane): job for job, lane in zip(batch, lanes)}
                    for f in as_completed(futures):
                        n, c = futures[f]
                        try:
                            r = dict(measured[(n['name'], c)])
                            r.update(f.result())
                            publish(n, c, r)
                        except Exception:
                            pass
                        with LOCK:
                            STATE['completed'] += 1
                            STATE['phase'] = f"阶段3：AI / 流媒体检测 {STATE['completed']}/{len(passed)}：{n['name']}"

        if (speed_mb or upload_mb) and passed and not CANCEL.is_set():
            def large(i, n, c, proxy):
                r = dict(measured[(n['name'], c)])
                key, country = f'{n["name"]}|{c}', node_country(n, r)
                if speed_mb:
                    res = speedtest.download_test(proxy, speed_mb, streams, country, CANCEL, live_reporter(key, n['name'], '下载', speed_mb))
                    clear_live(key)
                    traffic = r.get('traffic_download_bytes', 0) + res.pop('traffic_download_bytes', 0)
                    err = res.pop('error', '')
                    r.update(res)
                    r['traffic_download_bytes'] = traffic
                    r['error'] = err
                    r['stage'] = ('完整测速' if r.get('download_complete') else '限时采样：' + r.get('download_stop_reason', '')) if r.get('speed_mbps') is not None else '下载测速失败'
                if upload_mb and not CANCEL.is_set():
                    res = speedtest.upload_test(core.ports[i], upload_mb, streams, CANCEL, live_reporter(key, n['name'], '上传', upload_mb))
                    clear_live(key)
                    r.update(res)
                    r['upload_requested_mb'] = upload_mb
                r['measured_at'] = now()
                return r
            title = '阶段4：' + '+'.join(x for x in (f'{speed_mb}MB下载' if speed_mb else '', f'{upload_mb}MB上传' if upload_mb else '') if x) + f'（{streams}线程，逐个测）'
            phase(title, passed, 1, large)

        data['meta']['latency_passed'] = len(alive)
        data['meta']['small_passed'] = len(passed)
        data['meta']['finished'] = now()
        data['meta']['cancelled'] = CANCEL.is_set()
        for row in data['rows']:
            row['same_exit_count'] = sum(1 for r in data['rows'] if row.get('exit_ip') and r.get('exit_ip') == row['exit_ip'] and r['chain'] == row['chain'])
        data['rows'].sort(key=lambda r: (r['chain'], r.get('latency_ms') is None, r.get('latency_ms') or 0))
        (output / '原始结果.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        update(rows=list(data['rows']))
        if data['rows']:
            update(phase='正在生成 Excel 与彩色长图')
            files = export_report(data, output)
            update(files={k: '/files/' + str(pathlib.Path(v).relative_to(RESULTS)).replace('\\', '/') for k, v in files.items()},
                   phase='已停止，部分结果已保存' if CANCEL.is_set() else '测评完成')
        else:
            update(phase='已停止，尚无结果')
    except Exception as exc:
        update(error=str(exc), phase='测评未完成')
    finally:
        if core:
            core.close()
        update(running=False, downloads={})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ('/', '/app.js'):
            file, ctype = ('index.html', 'text/html') if path == '/' else ('app.js', 'text/javascript')
            b = (APP / file).read_text(encoding='utf-8').replace('__TOKEN__', TOKEN).encode()
            self.send_response(200)
            self.send_header('Content-Type', ctype + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b)
        elif path == '/api/profiles':
            try:
                self.send_json({'profiles': profiles(), 'clash_verge': (CLASH / 'profiles.yaml').is_file(), 'core': str(find_core() or '')})
            except Exception:
                self.send_json({'error': '无法读取 Clash Verge 订阅配置。'}, 500)
        elif path == '/api/nodes':
            uid = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('profile', [''])[0]
            try:
                physical, from_core = list_nodes(uid)
                _, cfg, _ = subscription(uid)
                self.send_json({'nodes': [{'name': p['name'], 'type': p.get('type', ''), **classify(p)} for p in physical],
                                'groups': group_nodes(physical), 'has_providers': bool(cfg.get('proxy-providers')) and not from_core})
            except Exception as exc:
                self.send_json({'error': f'无法读取节点：{exc}'[:300]}, 400)
        elif path == '/api/state':
            with LOCK:
                self.send_json(STATE)
        elif path.startswith('/files/'):
            p = (RESULTS / urllib.parse.unquote(path[len('/files/'):])).resolve()
            if not p.is_relative_to(RESULTS.resolve()) or not p.is_file():
                self.send_error(404)
                return
            types = {'.png': 'image/png', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.csv': 'text/csv; charset=utf-8', '.json': 'application/json'}
            self.send_response(200)
            self.send_header('Content-Type', types.get(p.suffix, 'application/octet-stream'))
            self.send_header('Content-Length', str(p.stat().st_size))
            self.end_headers()
            self.wfile.write(p.read_bytes())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.headers.get('X-Local-Token') != TOKEN:
            self.send_json({'error': '请刷新本地页面。'}, 403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 100000:
                raise ValueError('请求过大')
            obj = json.loads(self.rfile.read(length) or b'{}')
            if self.path == '/api/subscription':
                uid, name = add_url_subscription(str(obj.get('url', '')))
                self.send_json({'ok': True, 'id': uid, 'name': name})
            elif self.path == '/api/start':
                with LOCK:
                    if STATE['running']:
                        self.send_json({'error': '已有测评在运行。'}, 409)
                        return
                    if obj.get('mode') not in ('direct', 'chain', 'both'):
                        raise ValueError('无效测试方式')
                    if int(obj.get('speed_mb', 0)) not in (0, 20, 50, 100):
                        raise ValueError('无效下载测速流量')
                    if int(obj.get('upload_mb', 0)) not in (0, 10, 20, 50):
                        raise ValueError('无效上传测速流量')
                    if int(obj.get('streams', 4)) not in (1, 4):
                        raise ValueError('线程数只能选1或4')
                    if int(obj.get('concurrency', 16)) not in (8, 16, 32):
                        raise ValueError('并发只能选8、16或32')
                    obj = {k: obj[k] for k in ('profile', 'mode', 'speed_mb', 'upload_mb', 'streams', 'nodes', 'filter', 'dedupe', 'concurrency', 'endpoint_lookup') if k in obj}
                    STATE.update(running=True, completed=0, total=0, rows=[], files={}, error='', phase='准备中', options=dict(obj), run_key=now())
                    CANCEL.clear()
                threading.Thread(target=run_test, args=(obj,), daemon=True).start()
                self.send_json({'ok': True})
            elif self.path == '/api/retest':
                with LOCK:
                    if STATE['running']:
                        self.send_json({'error': '请等待当前测评结束。'}, 409)
                        return
                    mb = int(obj.get('speed_mb', 20))
                    if mb not in (20, 50, 100):
                        raise ValueError('下载量只能选20、50或100MB')
                    row = next((r for r in STATE['rows'] if r['node'] == obj.get('node') and r['chain'] == obj.get('chain')), None)
                    if not row:
                        raise ValueError('结果已更新，请刷新页面。')
                    prev = STATE.get('options', {})
                    uid = prev.get('profile')
                    if not uid:
                        raise ValueError('无法找到原订阅，请重新选择订阅测评。')
                    opts = {'profile': uid, 'nodes': [row['node']], 'mode': 'chain' if row['chain'] == '住宅链路' else 'direct', 'speed_mb': mb,
                            'upload_mb': int(obj.get('upload_mb', prev.get('upload_mb', 0)) or 0), 'streams': int(prev.get('streams', 4)), 'dedupe': False,
                            '_base_rows': json.loads(json.dumps(STATE['rows'])), '_base_meta': {'profile_id': uid}}
                    if opts['upload_mb'] not in (0, 10, 20, 50):
                        opts['upload_mb'] = 0
                    opts['endpoint_lookup'] = obj.get('endpoint_lookup') is True
                    opts['speed_only'] = obj.get('kind', 'speed') == 'speed'
                    STATE.update(running=True, completed=0, total=1, error='', phase='准备单节点重测')
                    CANCEL.clear()
                threading.Thread(target=run_test, args=(opts,), daemon=True).start()
                self.send_json({'ok': True})
            elif self.path == '/api/stop':
                CANCEL.set()
                self.send_json({'ok': True})
            else:
                self.send_error(404)
        except Exception as exc:
            self.send_json({'error': str(exc)[:300]}, 400)


def restore_last_results():
    saved = sorted(RESULTS.glob('*/原始结果.json'))
    if not saved:
        return
    try:
        last = saved[-1]
        data = json.loads(last.read_text(encoding='utf-8'))
        data['rows'] = [r for r in data.get('rows', []) if physical_node({'name': r.get('node', '')})]
        files = {}
        manifest = last.parent / '报告文件.json'
        if manifest.exists():
            for key, fname in json.loads(manifest.read_text(encoding='utf-8')).items():
                if (last.parent / fname).exists():
                    files[key] = '/files/' + str((last.parent / fname).relative_to(RESULTS)).replace('\\', '/')
        meta = data.get('meta', {})
        uid = meta.get('profile_id') or ''
        if uid and not uid.startswith('url-') and not any(p['id'] == uid for p in profiles()):
            uid = ''
        if uid.startswith('url-'):
            uid = ''   # pasted links are not remembered across restarts
        STATE.update(rows=data['rows'], completed=len(data['rows']), total=len(data['rows']), files=files, phase='上次测评结果',
                     run_key=meta.get('started'), options={'profile': uid, 'mode': meta.get('mode', 'direct'), 'speed_mb': meta.get('speed_mb', 0),
                                                           'upload_mb': meta.get('upload_mb', 0), 'streams': meta.get('streams', 4),
                                                           'dedupe': meta.get('dedupe', True), 'nodes': [], 'filter': ''})
    except Exception:
        pass


def main():
    if '--ensure-core' in sys.argv:
        ensure_core()
        return
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8775
    RESULTS.mkdir(exist_ok=True)
    RUNTIME.mkdir(exist_ok=True)
    # Leftovers from a crashed run (never touches anything outside runtime/).
    for d in RUNTIME.iterdir():
        if d.is_dir() and re.fullmatch(r'[0-9a-f]{16}', d.name):
            shutil.rmtree(d, ignore_errors=True)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    restore_last_results()
    print(f'http://127.0.0.1:{port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        CANCEL.set()


atexit.register(lambda: [core.close() for core in list(ACTIVE_CORES)])
if __name__ == '__main__':
    main()
