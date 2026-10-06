"""Conservative, unauthenticated streaming/AI region probes via curl.exe.

Results describe public landing-page and region-gate evidence only. They do
not prove that an account can sign in or that paid content can be played.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


_MAX_BODY = 3 * 1024 * 1024
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_CURL = shutil.which("curl.exe") or shutil.which("curl") or "curl.exe"

# Detection-source records, including links to the inspected scripts.
SOURCES = [
    {
        "name": "RegionRestrictionCheck",
        "url": "https://github.com/lmc999/RegionRestrictionCheck",
        "inspected": [
            "https://github.com/lmc999/RegionRestrictionCheck/blob/main/check.sh",
            "https://github.com/lmc999/RegionRestrictionCheck/commit/9e7d25b9b3e28a2c236fb20e0e5e50e8dfa3add5",
        ],
        "note": "Netflix double-title/Oh no marker; Gemini and Claude probes; used as reference, not copied as a full unlock claim.",
    },
    {
        "name": "miaospeed-scripts",
        "url": "https://github.com/CloudPassenger/miaospeed-scripts",
        "inspected": [
            "https://github.com/CloudPassenger/miaospeed-scripts/blob/main/src/checks/ai/chatgpt.ts",
            "https://github.com/CloudPassenger/miaospeed-scripts/blob/main/src/checks/ai/claude.ts",
            "https://github.com/CloudPassenger/miaospeed-scripts/blob/main/src/checks/ai/gemini.ts",
        ],
        "note": "Reviewed trace/region-gate methods; page 200 alone is insufficient for Gemini; ChatGPT uses its compliance cookie-requirements endpoint, not generic API reachability.",
    },
    {
        "name": "Netflix title probe references",
        "url": "https://github.com/lmc999/RegionRestrictionCheck/blob/main/check.sh",
        "inspected": [
            "https://www.netflix.com/title/81280792",
            "https://www.netflix.com/title/70143836",
        ],
        "note": "Community probes use LEGO Ninjago and Breaking Bad; results are conservatively labelled Originals Only / accessible / pending.",
    },
]


def _empty_result() -> dict[str, Any]:
    return {"code": 0, "body": "", "headers": {}, "error": "", "elapsed": 0.0, "bytes": 0, "url": ""}


def _safe_error(message: str, proxy_url: str) -> str:
    """Avoid returning a proxy URL, which may contain credentials, in errors."""
    safe = str(message or "")
    if proxy_url:
        safe = safe.replace(proxy_url, "<proxy>")
        parts = urlsplit(proxy_url)
        if parts.username or parts.password:
            secret = f"{parts.username or ''}:{parts.password or ''}@"
            safe = safe.replace(secret, "<credentials>@")
    # Be defensive around common user:password@ forms echoed by curl.
    safe = re.sub(r"(?i)(https?|socks5h?)://[^\s/@:]+:[^\s/@]+@", r"\1://<credentials>@", safe)
    return safe[:400]


def _parse_headers(raw: bytes) -> dict[str, str]:
    # dump-header includes interim/proxy/redirect blocks; keep final response.
    text = raw.decode("latin-1", errors="replace")
    blocks = re.split(r"\r?\n\r?\n", text)
    block = next((b for b in reversed(blocks) if b.lstrip().startswith("HTTP/")), "")
    result: dict[str, str] = {}
    for line in block.splitlines()[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key and key != "set-cookie":
            result[key] = value.strip()[:1000]
    return result


def fetch(
    url: str,
    proxy_url: str,
    timeout: float = 8,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
    data: str | bytes | None = None,
) -> dict[str, Any]:
    """Fetch a bounded response through an explicit HTTP/SOCKS5 proxy.

    No cookies or service-account authentication are added. Proxy credentials
    embedded in proxy_url are left to curl for proxy authentication only.
    """
    result = _empty_result()
    started = time.monotonic()
    try:
        scheme = urlsplit(proxy_url).scheme.lower()
        if scheme not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError("代理只支持 http(s) 或 socks5(socks5h) URL")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("目标 URL 必须是 HTTP 或 HTTPS")
        limit = max(1.0, min(float(timeout), 60.0))
        with tempfile.TemporaryDirectory(prefix="unlock-probe-") as td:
            body_path = str(Path(td) / "body.bin")
            headers_path = str(Path(td) / "headers.bin")
            args = [
                _CURL,
                "--silent", "--show-error", "--location", "--max-redirs", "5",
                "--proxy", proxy_url, "--noproxy", "",
                "--connect-timeout", str(min(limit, 8.0)), "--max-time", str(limit),
                "--max-filesize", str(_MAX_BODY),
                "--dump-header", headers_path, "--output", body_path,
                "--write-out", "%{http_code}|%{size_download}|%{time_total}|%{url_effective}",
                "--request", str(method or "GET").upper(),
                "--header", f"User-Agent: {_UA}",
            ]
            for key, value in (headers or {}).items():
                # Do not forward cookies or auth headers from a caller.
                # The literal "Bearer null" used by public region probes is not a credential.
                if str(key).lower() in {"cookie", "proxy-authorization"}:
                    continue
                if str(key).lower() == "authorization" and str(value) != "Bearer null":
                    continue
                args.extend(["--header", f"{key}: {value}"])
            input_data: bytes | None = None
            if data is not None:
                input_data = data if isinstance(data, bytes) else str(data).encode("utf-8")
                args.extend(["--data-binary", "@-"])
            args.append(url)
            too_large = False
            locally_timed_out = False
            with open(body_path, "wb") as body_file:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                try:
                    if input_data is not None and proc.stdin is not None:
                        proc.stdin.write(input_data)
                        proc.stdin.close()
                        proc.stdin = None
                    deadline = time.monotonic() + limit + 2
                    while proc.poll() is None:
                        if os.path.getsize(body_path) > _MAX_BODY:
                            too_large = True
                            proc.kill()
                            break
                        if time.monotonic() >= deadline:
                            locally_timed_out = True
                            proc.kill()
                            break
                        time.sleep(0.025)
                    stdout, stderr = proc.communicate(timeout=1)
                except Exception:
                    proc.kill()
                    proc.communicate()
                    raise
            if too_large:
                result["error"] = "response_too_large"
            elif locally_timed_out:
                result["error"] = "timeout"
            meta = stdout.decode("utf-8", errors="replace").strip().split("|", 3)
            if len(meta) >= 1 and meta[0].isdigit():
                result["code"] = int(meta[0])
            try:
                result["elapsed"] = float(meta[2]) if len(meta) > 2 else time.monotonic() - started
            except ValueError:
                result["elapsed"] = time.monotonic() - started
            if len(meta) > 3:
                result["url"] = meta[3].strip()[:500]
            try:
                raw_body = Path(body_path).read_bytes()
            except OSError:
                raw_body = b""
            # --max-filesize guards the transfer; clamp again before returning.
            raw_body = raw_body[:_MAX_BODY]
            result["bytes"] = len(raw_body)
            result["body"] = raw_body.decode("utf-8", errors="replace")
            try:
                result["headers"] = _parse_headers(Path(headers_path).read_bytes())
            except OSError:
                pass
            if proc.returncode and not result["error"]:
                if proc.returncode == 28:
                    result["error"] = "timeout"
                elif proc.returncode == 63:
                    result["error"] = "response_too_large"
                elif proc.returncode == 60:
                    result["error"] = "tls_verification_failed"
                else:
                    result["error"] = _safe_error(stderr.decode("utf-8", "replace").strip(), proxy_url) or f"curl_exit_{proc.returncode}"
            if result["code"] == 0 and proc.returncode == 0:
                result["error"] = "no_http_response"
    except subprocess.TimeoutExpired:
        result["error"] = "timeout"
    except FileNotFoundError:
        result["error"] = "curl.exe_unavailable"
    except Exception as exc:  # A bad probe must never break the whole batch.
        result["error"] = _safe_error(str(exc), proxy_url)
    result["elapsed"] = round(float(result["elapsed"] or (time.monotonic() - started)), 3)
    return result


def _out(status: str, detail: str, region: str | None = None) -> dict[str, str]:
    result = {"status": status, "detail": detail[:180]}
    if region:
        result["region"] = region
    return result


def _failure(res: Mapping[str, Any], label: str) -> dict[str, str] | None:
    code, error = int(res.get("code") or 0), str(res.get("error") or "")
    if code == 451 or any(marker in str(res.get('body','')).lower() for marker in ('unsupported_country','country_not_supported','not available in your country','not available in your region')):
        return _out('地区限制', label+'返回明确地区不可用标记')
    if error == "timeout":
        return _out("超时", f"{label}请求超时")
    if error or code == 0:
        return _out("待确认", f"{label}探测失败：{error or '无 HTTP 响应'}")
    if code in (403, 429):
        return _out("风控", f"HTTP {code}；可能是 IP 风控或请求限流，不能据此判断地区限制")
    return None


def _probe_chatgpt(proxy_url: str, timeout: float) -> dict[str, str]:
    """Same signals as RegionRestrictionCheck WebTest_OpenAI."""
    gate = fetch("https://api.openai.com/compliance/cookie_requirements", proxy_url, timeout, headers={
        "Authorization": "Bearer null", "Accept": "*/*", "Content-Type": "application/json",
        "Origin": "https://platform.openai.com", "Referer": "https://platform.openai.com/"})
    ios = fetch("https://ios.chat.openai.com/", proxy_url, timeout, headers={"Accept-Language": "en-US,en;q=0.9"})
    if gate.get("error") == "timeout" and ios.get("error") == "timeout":
        return _out("超时", "OpenAI 两个探测地址均超时")
    if not gate["body"] and not ios["body"]:
        return _out("待确认", f"OpenAI 探测无响应：{gate.get('error') or ios.get('error') or '空响应'}")
    blocked_api = "unsupported_country" in gate["body"].lower()
    blocked_app = "vpn" in ios["body"].lower()
    _, region = _trace_region(proxy_url, timeout, "chatgpt.com")
    if not blocked_api and not blocked_app:
        return _out("支持地区", "compliance 接口无 unsupported_country，iOS 入口无 VPN 拦截；未验证账号", region)
    if blocked_api and blocked_app:
        return _out("地区限制", "compliance 接口返回 unsupported_country，iOS 入口提示 VPN", region)
    if blocked_app:
        return _out("仅网页可用", "iOS 入口提示 VPN，网页端地理 gate 未拦截", region)
    return _out("仅App可用", "网页 compliance 接口返回 unsupported_country，iOS 入口未拦截", region)


def _trace_region(proxy_url: str, timeout: float, host: str) -> tuple[dict[str, Any], str]:
    res = fetch(f"https://{host}/cdn-cgi/trace", proxy_url, timeout)
    match = re.search(r"(?m)^loc=([A-Z]{2})$", res.get("body", ""))
    return res, match.group(1) if match else ""


def _probe_claude(proxy_url: str, timeout: float) -> dict[str, str]:
    """claude.ai redirects unsupported regions to anthropic.com/app-unavailable-in-region."""
    page = fetch("https://claude.ai/", proxy_url, timeout)
    final = (page.get("url") or "").lower()
    _, region = _trace_region(proxy_url, timeout, "claude.ai")
    if "app-unavailable-in-region" in final:
        return _out("地区限制", "claude.ai 跳转到 app-unavailable-in-region", region)
    if page.get("error") == "timeout" and not final:
        return _out("超时", "Claude 网页请求超时", region)
    if final.startswith("https://claude.ai"):
        note = f"HTTP {page['code']}" + ("（Cloudflare 人机验证，浏览器一般可通过）" if page["code"] == 403 else "")
        return _out("支持地区", f"未跳转到地区不可用页；{note}；未验证账号", region)
    return _out("待确认", f"最终地址 {final or '无'}，HTTP {page['code']} {page.get('error') or ''}", region)


def _probe_gemini(proxy_url: str, timeout: float) -> dict[str, str]:
    page = fetch("https://gemini.google.com/", proxy_url, timeout)
    failed = _failure(page, "Gemini 网页")
    if failed:
        return failed
    m = re.search(r',2,1,200,"([A-Z]{3})"', page["body"])
    region = m.group(1) if m else ""
    if "45631641,null,true" in page["body"]:
        return _out("支持地区", "页面含可用标记 45631641,null,true（RegionRestrictionCheck 同款判据）", region)
    if page["code"] == 200 and len(page["body"]) > 20000:
        return _out("疑似地区限制", "页面正常返回但缺少可用标记；谷歌改版也可能导致误判", region)
    return _out("待确认", f"Gemini 返回 HTTP {page['code']}，页面内容不完整", region)


def _probe_netflix(proxy_url: str, timeout: float) -> dict[str, str]:
    urls = (
        "https://www.netflix.com/title/81280792",  # LEGO Ninjago (Netflix original licence)
        "https://www.netflix.com/title/70143836",  # Breaking Bad (licensed)
    )
    hdr = {"Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    a, b = [fetch(u, proxy_url, timeout, headers=hdr) for u in urls]
    for res in (a, b):
        failed = _failure(res, "Netflix 标题页")
        if failed:
            return failed
    body_a, body_b = a["body"], b["body"]
    if not body_a or not body_b:
        return _out("待确认", "Netflix 标题页无可判读响应")
    oh_a, oh_b = "oh no!" in body_a.lower(), "oh no!" in body_b.lower()
    m = re.search(r'"id"\s*:\s*"([A-Z]{2})"[^{}]{0,400}?"countryName"', body_a)
    region = m.group(1) if m else ""
    if oh_a and oh_b:
        return _out("仅自制剧", "两个测试标题均显示 Oh no!（RegionRestrictionCheck 判为 Originals Only）", region)
    return _out("支持地区", "至少一个非自制测试标题可打开；未验证账号与播放", region)


def _probe_disney(proxy_url: str, timeout: float) -> dict[str, str]:
    page = fetch("https://www.disneyplus.com/", proxy_url, timeout)
    final = (page.get("url") or "").lower()
    if re.search(r"preview|unavailable", final):
        return _out("地区限制", f"跳转到 {final[:120]}")
    failed = _failure(page, "Disney+ 网页")
    if failed:
        return failed
    body_l = page["body"].lower()
    if any(s in body_l for s in ("not available in your region", "not available in your country")):
        return _out("地区限制", "Disney+ 页面含明确的地区不可用提示")
    m = re.search(r'"(?:countryCode|region)"\s*:\s*"([A-Z]{2})"', page["body"])
    if page["code"] == 200:
        return _out("可访问(未验证账号)", "入口未跳转到 unavailable/preview；未用令牌接口验证目录", m.group(1) if m else None)
    return _out("待确认", f"Disney+ 返回 HTTP {page['code']}")


def _probe_youtube_premium(proxy_url: str, timeout: float) -> dict[str, str]:
    page = fetch("https://www.youtube.com/premium", proxy_url, timeout, headers={"Accept-Language": "en-US,en;q=0.9"})
    failed = _failure(page, "YouTube Premium 页面")
    if failed:
        return failed
    body = page["body"]
    m = re.search(r'"INNERTUBE_CONTEXT_GL"\s*:\s*"([A-Z]{2})"', body) or re.search(r'"countryCode"\s*:\s*"([A-Z]{2})"', body)
    region = m.group(1) if m else ""
    if "www.google.cn" in body:
        return _out("地区限制", "页面指向 www.google.cn（中国大陆出口）", "CN")
    if re.search(r"premium is not available in your country", body, re.I):
        return _out("地区限制", "页面提示 Premium is not available in your country", region)
    if re.search(r"ad-free", body, re.I):
        return _out("支持地区", "页面含 ad-free 订阅介绍（RegionRestrictionCheck 同款判据）", region)
    return _out("待确认", f"HTTP {page['code']}，未找到可用/不可用标记", region)


def check_all(proxy_url: str, timeout: float = 12) -> dict[str, dict[str, str]]:
    """Check six public entry points concurrently; isolate every platform error."""
    probes = {
        "ChatGPT": _probe_chatgpt,
        "Claude": _probe_claude,
        "Gemini": _probe_gemini,
        "Netflix": _probe_netflix,
        "Disney+": _probe_disney,
        "YouTube Premium": _probe_youtube_premium,
    }
    results: dict[str, dict[str, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fn, proxy_url, timeout): name for name, fn in probes.items()}
        for future, name in ((future, futures[future]) for future in futures):
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = _out("待确认", f"探测异常：{_safe_error(str(exc), proxy_url)}")
    urls={'ChatGPT':'https://api.openai.com/compliance/cookie_requirements','Claude':'https://claude.ai/','Gemini':'https://gemini.google.com/','Netflix':'https://www.netflix.com/title/81280792','Disney+':'https://www.disneyplus.com/','YouTube Premium':'https://www.youtube.com/premium'}
    for name, result in results.items():
        result['test_url']=urls[name]
        result['source_url']='https://github.com/lmc999/RegionRestrictionCheck/blob/main/check.sh'
    return results


if __name__ == "__main__":
    # Small, offline interface self-check; never contacts a service.
    assert set(check_all.__annotations__) >= {"proxy_url", "timeout", "return"}
    assert set(fetch("https://example.invalid", "bad-proxy").keys()) == {
        "code", "body", "headers", "error", "elapsed", "bytes", "url"
    }
    print("unlock_checks interface self-check: OK")
