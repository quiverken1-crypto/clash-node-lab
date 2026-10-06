"""Download / upload throughput tests through a local mixed (HTTP CONNECT) proxy.

Design notes (why the numbers look the way they do):

* Every node is tested against a source list chosen from the node's own
  region, never from what the previous node happened to use. Comparing a Hong
  Kong node against a server in Paris mostly measures the Paris route.
* Throughput is reported as a *steady-state* rate: the first part of the
  transfer (TCP slow start through a high-latency proxy) is discarded, the
  same way speed-test sites do. The plain average from the first byte is kept
  as a secondary figure.
* Several parallel streams (default 4) are used, like browser speed tests. A
  single TCP stream through a 300 ms proxy is often window/loss limited and
  understates what the node can deliver. Choose 1 stream to see single-file
  download behaviour.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

CURL = shutil.which('curl.exe') or shutil.which('curl') or 'curl.exe'
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36')

CONNECT_TIMEOUT = 10.0      # seconds to wait for the first byte
MIN_RATE_MBPS = 16          # download time limit = size / 2 MB/s
WARMUP_MAX_S = 2.0          # at most this much ramp-up is discarded
WARMUP_FRACTION = 0.25      # ...and never more than a quarter of the transfer

# name, url ({size} is replaced for size-addressable endpoints), max bytes
SOURCES = {
    'cachefly':    ('CacheFly 全球CDN', 'https://cachefly.cachefly.net/100mb.test', 100_000_000),
    'cloudflare':  ('Cloudflare 全球Anycast', 'https://speed.cloudflare.com/__down?bytes={size}', 100_000_000),
    'vultr_tokyo': ('Vultr 东京', 'https://hnd-jp-ping.vultr.com/vultr.com.1000MB.bin', 1_000_000_000),
    'vultr_sgp':   ('Vultr 新加坡', 'https://sgp-ping.vultr.com/vultr.com.1000MB.bin', 1_000_000_000),
    'vultr_lax':   ('Vultr 洛杉矶', 'https://lax-ca-us-ping.vultr.com/vultr.com.1000MB.bin', 1_000_000_000),
    'hetzner_sin': ('Hetzner 新加坡', 'https://sin-speed.hetzner.com/1GB.bin', 1_000_000_000),
    'hetzner_ash': ('Hetzner 美东', 'https://ash-speed.hetzner.com/1GB.bin', 1_000_000_000),
    'hetzner_fsn': ('Hetzner 德国', 'https://fsn1-speed.hetzner.com/1GB.bin', 1_000_000_000),
    'ovh':         ('OVH 法国', 'https://proof.ovh.net/files/1Gb.dat', 1_000_000_000),
}

EAST_ASIA = {'HK', 'MO', 'TW', 'JP', 'KR'}
SOUTH_ASIA = {'SG', 'MY', 'TH', 'VN', 'PH', 'ID', 'IN', 'KH', 'PK', 'AU', 'NZ'}
AMERICAS = {'US', 'CA', 'MX', 'BR', 'AR', 'CL', 'CO'}
EUROPE = {'GB', 'DE', 'FR', 'NL', 'PL', 'CH', 'FI', 'SE', 'IT', 'ES', 'RU', 'TR', 'UA', 'IE', 'AT', 'BE',
          'NO', 'DK', 'CZ', 'RO', 'PT', 'EG', 'NG', 'ZA', 'AE', 'IL'}


def sources_for(country: str) -> list[str]:
    """Source order for a node, chosen only from the node's region."""
    c = (country or '').upper()
    if c in EAST_ASIA:
        return ['cachefly', 'vultr_tokyo', 'hetzner_sin', 'cloudflare']
    if c in SOUTH_ASIA:
        return ['cachefly', 'hetzner_sin', 'vultr_sgp', 'cloudflare']
    if c in AMERICAS:
        return ['cachefly', 'vultr_lax', 'hetzner_ash', 'cloudflare']
    if c in EUROPE:
        return ['cachefly', 'hetzner_fsn', 'ovh', 'cloudflare']
    return ['cachefly', 'cloudflare', 'vultr_tokyo', 'hetzner_fsn']


def _split(total: int, parts: int) -> list[int]:
    parts = max(1, min(parts, total))
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def summarize(samples: list[tuple[float, int]], first: float | None, end: float, amount: int) -> dict:
    """Steady-state and average Mbps from (elapsed, cumulative_bytes) samples."""
    if first is None or amount <= 0:
        return {'steady_mbps': None, 'avg_mbps': None, 'transfer_s': 0.0}
    transfer = max(end - first, 1e-3)
    avg = amount * 8 / 1e6 / transfer
    warm_at = first + min(WARMUP_MAX_S, WARMUP_FRACTION * transfer)
    steady = avg
    if end - warm_at >= 0.5:
        warm_bytes = 0
        for (t0, b0), (t1, b1) in zip(samples, samples[1:]):
            if t1 >= warm_at:
                ratio = 0 if t1 == t0 else (warm_at - t0) / (t1 - t0)
                warm_bytes = b0 + (b1 - b0) * max(0.0, min(1.0, ratio))
                break
        if amount > warm_bytes:
            steady = (amount - warm_bytes) * 8 / 1e6 / (end - warm_at)
    return {'steady_mbps': round(steady, 2), 'avg_mbps': round(avg, 2), 'transfer_s': round(transfer, 3)}


def _live(samples, now):
    window = [s for s in samples if now - s[0] <= 2.0]
    if len(window) < 2 or window[-1][0] <= window[0][0]:
        return 0.0
    return (window[-1][1] - window[0][1]) * 8 / 1e6 / (window[-1][0] - window[0][0])


# --------------------------------------------------------------------------- download

def _download_source(proxy: str, size: int, streams: int, key: str, cancel: threading.Event,
                     report: Callable[[dict], None], transfer_limit: float) -> dict:
    name, url_tpl, max_bytes = SOURCES[key]
    size = min(size, max_bytes)
    with tempfile.TemporaryDirectory(prefix='node-dl-') as tmp:
        procs, files, offset = [], [], 0
        for i, part in enumerate(_split(size, streams)):
            body = Path(tmp) / f'part{i}.bin'
            url = url_tpl.format(size=part)
            cmd = [CURL, '--proxy', proxy, '--noproxy', '', '-sS', '-o', str(body), '-w', '%{http_code}',
                   '--connect-timeout', str(int(CONNECT_TIMEOUT)), '--max-time', str(int(CONNECT_TIMEOUT + transfer_limit + 5)),
                   '-H', 'Accept-Encoding: identity', '-A', UA]
            if '{size}' not in url_tpl:
                cmd += ['--range', f'{offset}-{offset + part - 1}']
            offset += part
            cmd.append(url)
            procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=FLAGS))
            files.append(body)
        started = time.monotonic()
        samples: list[tuple[float, int]] = [(0.0, 0)]
        first = None
        reason = ''
        amount = 0
        while True:
            now = time.monotonic() - started
            amount = sum(f.stat().st_size for f in files if f.exists())
            if amount and first is None:
                first = now
            samples.append((now, amount))
            report({'source': name, 'done_bytes': amount, 'target_bytes': size, 'elapsed_s': now,
                    'current_mbps': _live(samples, now),
                    'average_mbps': amount * 8 / 1e6 / max(now - (first or now), 1e-3) if first is not None else 0})
            if amount >= size:
                reason = 'complete'
                break
            if all(p.poll() is not None for p in procs):
                break
            if cancel.is_set():
                reason = '用户停止'
                break
            if first is None and now >= CONNECT_TIMEOUT + 1:
                reason = f'连接/首字节等待超过{CONNECT_TIMEOUT:g}秒'
                break
            if first is not None and now - first >= transfer_limit:
                reason = f'到达下载时限（{transfer_limit:g}秒）'
                break
            time.sleep(0.05)
        end = samples[-1][0]
        codes = []
        for p in procs:
            if p.poll() is None:
                p.terminate()
            try:
                out, _ = p.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                out, _ = p.communicate()
            code = out.decode('ascii', 'replace').strip()[-3:]
            codes.append(code if code.isdigit() else '000')
        amount = sum(f.stat().st_size for f in files if f.exists())
    bad = [c for c in codes if c not in ('200', '206', '000')]
    complete = amount >= size * 0.99
    stats = summarize(samples, first, end, amount)
    error = ''
    if bad and amount == 0:
        error = f'测速 HTTP {bad[0]}'
    elif amount == 0:
        error = reason or '没有收到下载数据'
    elif not complete:
        error = reason if reason and reason != 'complete' else '下载中断'
    return {'source_key': key, 'source': name, 'source_url': url_tpl.format(size=size), 'bytes': amount,
            'target_bytes': size, 'complete': complete, 'error': error, 'first_byte_s': first,
            'http_codes': codes, **stats}


def download_test(proxy: str, mb: int, streams: int, country: str, cancel: threading.Event,
                  report: Callable[[dict], None] = lambda _: None, transfer_limit: float | None = None) -> dict:
    """Try region-ordered sources until one yields data. Partial (time-limited) data is accepted."""
    size = int(mb * 1_000_000)
    limit = transfer_limit if transfer_limit is not None else max(5.0, size * 8 / 1e6 / MIN_RATE_MBPS)
    attempts, traffic = [], 0
    result = None
    for key in sources_for(country):
        if cancel.is_set():
            break
        result = _download_source(proxy, size, streams, key, cancel, report, limit)
        traffic += result['bytes']
        attempts.append({'source': result['source'], 'error': result['error'], 'received_bytes': result['bytes']})
        # Move on only when this source gave nothing usable (HTTP error, blocked, no data).
        if result['bytes'] > 0:
            break
    if result is None:
        return {'speed_mbps': None, 'error': '用户停止', 'traffic_download_bytes': 0, 'source_attempts': []}
    ok = result['bytes'] > 0 and not cancel.is_set()
    return {
        'speed_mbps': result['steady_mbps'] if ok else None,
        'speed_avg_mbps': result['avg_mbps'] if ok else None,
        'download_mb': round(result['bytes'] / 1e6, 2),
        'download_complete': result['complete'],
        'download_elapsed_s': result['transfer_s'],
        'download_first_byte_s': None if result['first_byte_s'] is None else round(result['first_byte_s'], 3),
        'download_stop_reason': '' if result['complete'] else result['error'],
        'download_streams': streams,
        'speed_source': result['source'],
        'speed_source_url': result['source_url'],
        'source_attempts': attempts,
        'traffic_download_bytes': traffic,
        'speed_basis': f'{streams}线程；去掉开头≤{WARMUP_MAX_S:g}秒爬升期后的稳态速率（平均值另列）',
        'error': '' if ok else ('所有测速源均未返回数据：' + '；'.join(f"{a['source']} {a['error']}" for a in attempts)),
    }


# --------------------------------------------------------------------------- upload

UPLOAD_CHUNK = os.urandom(64 * 1024)   # random bytes: nothing on the path can compress them


def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        import certifi  # optional, helps on systems with an outdated store
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def _upload_stream(proxy_host: str, proxy_port: int, url: str, nbytes: int, idx: int, progress: list,
                   status: list, cancel: threading.Event, deadline: list):
    parts = urlsplit(url)
    https = parts.scheme == 'https'
    port = parts.port or (443 if https else 80)
    try:
        if https:
            conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=CONNECT_TIMEOUT, context=_ssl_context())
        else:
            conn = http.client.HTTPConnection(proxy_host, proxy_port, timeout=CONNECT_TIMEOUT)
        conn.set_tunnel(parts.hostname, port)
        conn.connect()
        # Keep local buffering small so "bytes handed to the socket" stays close to "bytes on the wire".
        try:
            conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024)
        except OSError:
            pass
        conn.sock.settimeout(30)
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        conn.putrequest('POST', path, skip_accept_encoding=True)
        conn.putheader('User-Agent', UA)
        conn.putheader('Content-Type', 'application/octet-stream')
        conn.putheader('Content-Length', str(nbytes))
        conn.endheaders()
        sent = 0
        view = memoryview(UPLOAD_CHUNK)
        while sent < nbytes:
            if cancel.is_set():
                status[idx] = ('cancel', 0)
                conn.close()
                return
            if deadline[0] and time.monotonic() > deadline[0]:
                status[idx] = ('limit', 0)
                conn.close()
                return
            n = min(len(view), nbytes - sent)
            conn.sock.sendall(view[:n])
            sent += n
            progress[idx] = sent
        resp = conn.getresponse()
        resp.read(65536)
        status[idx] = ('done', resp.status)
        conn.close()
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        status[idx] = ('error', type(exc).__name__ + ': ' + str(exc)[:120])


def _upload_target(proxy_host, proxy_port, key):
    if key == 'cloudflare':
        return 'Cloudflare 全球Anycast', 'https://speed.cloudflare.com/__up', 50_000_000
    # Ookla's server list is geo-located from the requesting (exit) IP.
    conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=CONNECT_TIMEOUT, context=_ssl_context())
    conn.set_tunnel('www.speedtest.net', 443)
    conn.request('GET', '/api/js/servers?engine=js&https_functional=true&limit=5', headers={'User-Agent': UA})
    servers = json.loads(conn.getresponse().read(200000) or b'[]')
    conn.close()
    for s in servers:
        url = s.get('url') or ''
        if s.get('https_functional') and url.startswith('http://'):
            url = 'https://' + url[len('http://'):]
        if url:
            return f"Ookla {s.get('sponsor', '')} {s.get('name', '')}".strip(), url, 50_000_000
    raise RuntimeError('没有可用的 Ookla 服务器')


def _upload_once(proxy_host, proxy_port, name, url, size, streams, cancel, report, transfer_limit):
    parts = _split(size, streams)
    progress = [0] * len(parts)
    status: list = [None] * len(parts)
    deadline = [0.0]
    threads = [threading.Thread(target=_upload_stream, args=(proxy_host, proxy_port, url, n, i, progress, status, cancel, deadline), daemon=True)
               for i, n in enumerate(parts)]
    started = time.monotonic()
    for t in threads:
        t.start()
    samples = [(0.0, 0)]
    first = None
    while any(t.is_alive() for t in threads):
        now = time.monotonic() - started
        amount = sum(progress)
        if amount and first is None:
            first = now
            deadline[0] = time.monotonic() + transfer_limit
        if first is None and now >= CONNECT_TIMEOUT + 1:
            deadline[0] = time.monotonic()   # give up connecting
        samples.append((now, amount))
        report({'source': name, 'done_bytes': amount, 'target_bytes': size, 'elapsed_s': now,
                'current_mbps': _live(samples, now),
                'average_mbps': amount * 8 / 1e6 / max(now - (first or now), 1e-3) if first is not None else 0})
        time.sleep(0.05)
    end = time.monotonic() - started
    amount = sum(progress)
    samples.append((end, amount))
    kinds = [s[0] if s else 'error' for s in status]
    codes = [s[1] for s in status if s and s[0] == 'done']
    errors = [s[1] for s in status if s and s[0] == 'error']
    complete = all(k == 'done' for k in kinds) and all(200 <= int(c) < 300 for c in codes) and amount >= size
    error = ''
    if codes and any(int(c) >= 400 for c in codes):
        error = f'上传 HTTP {next(c for c in codes if int(c) >= 400)}'
    elif 'cancel' in kinds:
        error = '用户停止'
    elif 'limit' in kinds:
        error = f'到达上传时限（{transfer_limit:g}秒）'
    elif errors:
        error = errors[0]
    stats = summarize(samples, first, end, amount)
    return {'source': name, 'url': url, 'bytes': amount, 'complete': complete, 'error': error, **stats}


def upload_test(proxy_port: int, mb: int, streams: int, cancel: threading.Event,
                report: Callable[[dict], None] = lambda _: None, proxy_host: str = '127.0.0.1') -> dict:
    """POST random bytes to Cloudflare (then the nearest Ookla server) through the node.

    The clock stops when the server has answered, so data still sitting in
    local buffers is not counted as uploaded. If the time limit cuts the upload
    short the rate is marked 限时采样 and may be slightly optimistic.
    """
    size = int(mb * 1_000_000)
    limit = max(10.0, size * 8 / 1e6 / 8)      # allow down to 8 Mbps before cutting off
    attempts, traffic, result = [], 0, None
    for key in ('cloudflare', 'ookla'):
        if cancel.is_set():
            break
        try:
            name, url, max_bytes = _upload_target(proxy_host, proxy_port, key)
        except Exception as exc:  # noqa: BLE001
            attempts.append({'source': key, 'error': '获取上传服务器失败：' + type(exc).__name__, 'sent_bytes': 0})
            continue
        result = _upload_once(proxy_host, proxy_port, name, url, min(size, max_bytes), streams, cancel, report, limit)
        traffic += result['bytes']
        attempts.append({'source': name, 'error': result['error'], 'sent_bytes': result['bytes']})
        if result['bytes'] > 0 and 'HTTP' not in result['error']:
            break
    ok = bool(result) and result['bytes'] > 0 and not cancel.is_set() and 'HTTP' not in result['error']
    return {
        # Sender-side byte counters run ahead of the network while local buffers fill,
        # so a "steady" window is unreliable here; the first-byte→server-reply average is not.
        'upload_mbps': result['avg_mbps'] if ok else None,
        'upload_avg_mbps': result['avg_mbps'] if ok else None,
        'upload_mb': round(result['bytes'] / 1e6, 2) if result else 0,
        'upload_complete': bool(result and result['complete']),
        'upload_elapsed_s': result['transfer_s'] if result else 0,
        'upload_stop_reason': '' if (result and result['complete']) else (result['error'] if result else '用户停止'),
        'upload_source': result['source'] if result else '',
        'upload_attempts': attempts,
        'traffic_upload_bytes': traffic,
        'upload_error': '' if ok else ('上传测速失败：' + '；'.join(f"{a['source']} {a['error']}" for a in attempts)),
    }
