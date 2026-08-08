"""P123DiskMulti 分享票（share/download/info）下载通道

背景：VIP 直链换链通道（yun/api.123278）存在账号级风控，STRM 302 播放模式下
每次播放=一条 VIP 直链被外部 IP 下载，长期运营必然反复触发 50002/1010。
分享下载本就是「任何人免登录、多 IP 下载」的设计内行为，多 IP 播放不再是
异常特征；最坏情况是单条分享链接被风控（重建分享即可），账号不受影响。

链路（2026-08-08 实测验证）：
1. POST {SHARE_API_BASE}/api/share/download/info（需 Authorization: Bearer 任意已登录账号 token）
   → data.DownloadURL = https://web-pro2.123952.com/download-v2/?params=<base64>
   params 直接 base64 解码即得真实 CDN 票 URL（无需请求中转页）
2. GET 内嵌 CDN 票（带 Range，auto_redirect=0）→ 网关返回 HTTP 210
   {"code":0,"data":{"redirect_url":"https://{IP}-v3.pd1.cjjd19.com/..."}}
3. GET redirect_url（带 Range）→ 边缘节点 HTTP 206，支持断点续传

设计：
- 缓存「内嵌 CDN 票」而非最终边缘 URL：边缘节点可能按请求轮换，票是
  换票频率的瓶颈；每次播放只多一次 210 解析 GET（换票 POST 被缓存吸收）
- 票有效期按 URL 中 t 参数计算，过期自动重新换票；缓存上限 256 条
- 210 解析遇 403（通道风控/地域绑定）时丢弃缓存并重新换票重试一次
  （新票身份不同；若仍 403 则整通道风控，明确日志提示）
"""

import base64
import threading
import time
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import requests

from app.log import logger

# 分享票换票网关（与网页前端同域，实测该域签发的票 ur=vpngvaegvngvnp 正常家族）
SHARE_API_BASE = "https://api.123278.com/b"
# 网页前端请求头模板（实测必需：platform: web + App-Version）
WEB_PLATFORM = "web"
WEB_APP_VERSION = "132"
# 默认 User-Agent（未传入时使用）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 票有效期安全余量（秒）：t 前 5 分钟即视为过期，避免播放中票失效
_TICKET_EXPIRY_MARGIN = 300
# t 解析失败时的兜底缓存时长（秒）
_DEFAULT_TTL = 600
# 缓存条目上限
_MAX_CACHE = 256


def _web_headers(user_agent: str, token: str) -> dict:
    """分享票请求头（网页前端模板 + 登录态）。"""
    return {
        "User-Agent": user_agent or DEFAULT_UA,
        "platform": WEB_PLATFORM,
        "App-Version": WEB_APP_VERSION,
        "Authorization": f"Bearer {token}" if token else "",
    }


def exchange_share_ticket(
    token: str,
    share_key: str,
    share_pwd: str,
    file_id,
    s3_key_flag: str,
    etag: str,
    size,
    user_agent: str = "",
    timeout: float = 8,
) -> Optional[str]:
    """
    换取分享下载票：POST share/download/info → 解码 params 得内嵌 CDN 票 URL

    失败返回 None 并输出明确错误日志（400 分享失效 / 5112 需登录 / 风控等）。
    """
    try:
        file_id_int = int(file_id)
        size_int = int(size or 0)
    except (TypeError, ValueError):
        logger.error(
            f"【123多盘】分享票换票参数非法: fileId={file_id!r} size={size!r}"
        )
        return None
    if not token:
        logger.error(
            "【123多盘】分享票换票缺少登录态：未取得账号 token"
            "（123 要求分享下载携带任意已登录账号的 Authorization）"
        )
        return None
    payload = {
        "shareKey": str(share_key),
        "sharePwd": str(share_pwd or ""),
        "fileId": file_id_int,
        "s3KeyFlag": str(s3_key_flag),
        "etag": str(etag),
        "size": size_int,
    }
    try:
        resp = requests.post(
            f"{SHARE_API_BASE}/api/share/download/info",
            json=payload,
            headers=_web_headers(user_agent, token),
            timeout=timeout,
        )
    except requests.RequestException as e:
        logger.warn(f"【123多盘】分享票换票请求异常: {type(e).__name__}: {e}")
        return None
    try:
        body = resp.json()
    except Exception:
        logger.warn(
            f"【123多盘】分享票换票响应异常（HTTP {resp.status_code}）: "
            f"{(resp.text or '')[:200]}"
        )
        return None
    code = body.get("code")
    message = str(body.get("message") or "").strip()
    if code not in (0, None):
        # 明确错误码日志（验收标准 3：5112/400/50002 需可辨识）
        if code == 5112 or "登录" in message:
            logger.error(
                f"【123多盘】分享下载要求登录态（code=5112 {message}），"
                f"请检查插件账号登录状态"
            )
        elif code == 400 or "源文件不存在" in message:
            logger.error(
                f"【123多盘】分享源文件不存在（code=400 {message}）："
                f"分享可能已失效/已重建，请更新分享链接或重新转存"
            )
        elif str(code) in ("1010", "50001", "50002") or "download err" in message:
            logger.error(
                f"【123多盘】分享下载通道被风控（code={code} {message}）"
            )
        else:
            logger.error(f"【123多盘】分享票换票失败: code={code} {message}")
        return None
    data = body.get("data") or {}
    dl_url = data.get("DownloadURL") or data.get("downloadURL") or ""
    if not dl_url:
        logger.warn(
            f"【123多盘】分享票换票成功但响应缺少 DownloadURL: {str(body)[:200]}"
        )
        return None
    params = (parse_qs(urlsplit(str(dl_url)).query).get("params") or [""])[0]
    if not params:
        logger.warn(f"【123多盘】分享票下载页缺少 params 参数: {dl_url[:200]}")
        return None
    try:
        params += "=" * (-len(params) % 4)
        inner = base64.b64decode(params.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.warn(f"【123多盘】分享票 params 解码失败: {e}")
        return None
    if not inner.startswith(("http://", "https://")):
        logger.warn(f"【123多盘】分享票解码结果不是 URL: {inner[:200]}")
        return None
    logger.debug(f"【123多盘】分享票换票成功: {inner[:200]}")
    return inner


def ticket_ttl(
    inner_url: str,
    default: int = _DEFAULT_TTL,
    max_ttl: int = 6 * 3600,
) -> int:
    """
    按票 URL 中 t 参数（过期时间戳）计算缓存时长（秒）

    t 缺失/解析失败用默认值；剩余有效期不足安全余量返回 0（不缓存）。
    """
    try:
        t = int(
            (parse_qs(urlsplit(inner_url).query).get("t") or [""])[0]
        )
    except (TypeError, ValueError):
        t = 0
    if not t:
        return default
    remain = t - time.time() - _TICKET_EXPIRY_MARGIN
    if remain <= 0:
        return 0
    return int(min(remain, max_ttl))


def resolve_share_cdn(
    inner_url: str,
    user_agent: str = "",
    timeout: float = 8,
) -> Tuple[Optional[str], int, str]:
    """
    解析分享票内嵌 CDN 票 → 最终边缘 URL

    :return: (url, status, detail)
        - url 非 None：210 JSON redirect_url / 30x Location / 200-206 直连
        - status=403：通道风控或地域绑定（detail 为解析出的内容）
        - status=0：网络异常
    """
    headers = {
        "Range": "bytes=0-0",
        "Connection": "close",
        "User-Agent": user_agent or DEFAULT_UA,
    }
    try:
        resp = requests.get(
            inner_url,
            headers=headers,
            stream=True,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        return None, 0, f"{type(e).__name__}: {e}"
    try:
        status = resp.status_code
        if status == 210:
            try:
                body = resp.json()
                url = (body.get("data") or {}).get("redirect_url") or ""
                if url:
                    return url, status, ""
                return None, status, f"210 响应缺少 redirect_url: {str(body)[:200]}"
            except Exception as e:
                return None, status, f"210 响应解析失败: {e}"
        if status in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location") or ""
            return (loc or None), status, ("" if loc else "30x 缺少 Location")
        if status in (200, 206):
            return inner_url, status, ""
        detail = ""
        if status == 403:
            try:
                body = resp.json()
                detail = (
                    f"message={body.get('message') or ''} "
                    f"code={body.get('code') or ''}"
                ).strip()
            except Exception:
                detail = (resp.text or "")[:200]
        return None, status, detail
    except Exception as e:
        return None, 0, f"响应解析异常: {e}"
    finally:
        try:
            resp.close()
        except Exception:
            pass


class ShareTicketCache:
    """
    分享票缓存（内存，线程安全）：文件标识 -> (内嵌 CDN 票, 过期时间戳)

    只缓存票（换票 POST 是频率瓶颈），每次播放仍做一次 210 解析 GET，
    保证边缘节点 URL 按请求新鲜度返回。
    """

    def __init__(self):
        self._cache: Dict[tuple, Tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple) -> Optional[str]:
        if not key:
            return None
        now = time.time()
        with self._lock:
            item = self._cache.get(key)
            if item and item[1] > now:
                return item[0]
            if item:
                self._cache.pop(key, None)
        return None

    def set(self, key: tuple, inner_url: str, ttl: int):
        if not key or ttl <= 0:
            return
        with self._lock:
            if len(self._cache) >= _MAX_CACHE:
                now = time.time()
                stale = [k for k, (_, exp) in self._cache.items() if exp <= now]
                for k in stale:
                    self._cache.pop(k, None)
                if len(self._cache) >= _MAX_CACHE:
                    oldest = min(self._cache.items(), key=lambda kv: kv[1][1])
                    self._cache.pop(oldest[0], None)
            self._cache[key] = (inner_url, time.time() + ttl)

    def drop(self, key: tuple):
        if not key:
            return
        with self._lock:
            self._cache.pop(key, None)


def get_share_download_url(
    token: str,
    share_key: str,
    share_pwd: str,
    file_id,
    s3_key_flag: str,
    etag: str,
    size,
    user_agent: str = "",
    cache: Optional[ShareTicketCache] = None,
    timeout: float = 8,
) -> Optional[str]:
    """
    分享票完整链路：缓存命中 → 210 解析；未命中 → 换票 → 缓存 → 210 解析

    210 解析遇 403（风控）时丢弃缓存、重新换票重试一次（票轮换策略，
    应对单票被标记）；整通道风控时输出明确日志后返回 None。
    :return: 可直接 302 给 Emby 的最终边缘 URL；失败 None
    """
    key = (str(share_key), str(file_id), str(etag), int(size or 0))
    inner = cache.get(key) if cache else None
    if inner is None:
        inner = exchange_share_ticket(
            token, share_key, share_pwd, file_id, s3_key_flag,
            etag, size, user_agent=user_agent, timeout=timeout,
        )
        if not inner:
            return None
        if cache:
            cache.set(key, inner, ticket_ttl(inner))
    url, status, detail = resolve_share_cdn(inner, user_agent, timeout)
    if url:
        return url
    if status == 403 and ("realc" in detail or "city" in detail):
        logger.error(
            f"【123多盘】分享 CDN 地域绑定拦截（HTTP 403 {detail}）："
            f"下载出口 IP 与分享方地域不一致，请检查 MoviePilot/Emby 网络出口"
        )
        if cache:
            cache.drop(key)
        return None
    if status == 403:
        # 通道风控：丢弃本票缓存，重新换票重试一次（新票身份不同）
        logger.error(
            f"【123多盘】分享下载通道被风控（HTTP 403 {detail}），正在重新换票重试"
        )
        if cache:
            cache.drop(key)
        inner2 = exchange_share_ticket(
            token, share_key, share_pwd, file_id, s3_key_flag,
            etag, size, user_agent=user_agent, timeout=timeout,
        )
        if not inner2:
            return None
        url2, status2, detail2 = resolve_share_cdn(inner2, user_agent, timeout)
        if url2:
            # 仅验证通过的新票才入缓存（避免缓存被风控的票）
            if cache:
                cache.set(key, inner2, ticket_ttl(inner2))
            logger.warn(
                "【123多盘】重新换票后分享下载恢复（原票被标记，已轮换新票）"
            )
            return url2
        if status2 == 403:
            logger.error(
                f"【123多盘】分享下载通道风控持续（HTTP 403 {detail2}）："
                f"建议重建分享链接；若仍失败说明分享通道整体受限"
            )
        return None
    # 网络异常/其他状态：不销毁缓存（票本身可能有效），返回 None
    logger.warn(
        f"【123多盘】分享票解析异常（HTTP {status or '网络错误'} {detail}），"
        f"下次请求将重试"
    )
    return None
