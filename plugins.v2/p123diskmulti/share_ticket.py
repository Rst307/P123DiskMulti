"""P123DiskMulti 分享票（share/download/info）下载通道

背景：VIP 直链换链通道（yun/api.123278）存在账号级风控，STRM 302 播放模式下
每次播放=一条 VIP 直链被外部 IP 下载，长期运营必然反复触发 50002/1010。
分享下载本就是「任何人免登录、多 IP 下载」的设计内行为，多 IP 播放不再是
异常特征；最坏情况是单条分享链接被风控（重建分享即可），账号不受影响。

链路（2026-08-08 实测验证）：
1. POST {SHARE_API_BASE}/api/share/download/info（需 Authorization: Bearer 任意已登录账号 token；
   登录态校验失败（5112）时依次尝试 open_platform/android 平台模板，与本插件 token 体系匹配）
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
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import requests

from app.log import logger
from p123client import P123AuthenticationError, check_response

# 分享票换票网关（与网页前端同域，实测该域签发的票 ur=vpngvaegvngvnp 正常家族）
SHARE_API_BASE = "https://api.123278.com/b"
# 网页前端请求头模板（实测必需：platform: web + App-Version）
WEB_PLATFORM = "web"
WEB_APP_VERSION = "132"
# 分享票换票请求头模板（按序尝试，仅登录态校验失败时切换下一档）：
# - web：网页前端模板（v1.4.5 实测：小文件/免登录流量未告罄时可用）
# - open_platform：与 VIP 换链同款（api.123278.com/b 实测可用；大文件需登录时仍可换票）
# - android：社区 p123client 同款（open 平台 token + android 头，社区下载器日常验证）
_TICKET_HEADER_STYLES = (
    {"platform": WEB_PLATFORM, "App-Version": WEB_APP_VERSION},
    {"platform": "open_platform", "app-version": "3"},
    {"platform": "android", "app-version": "3"},
)
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


def _web_headers(user_agent: str) -> dict:
    """分享票请求头（网页前端模板）。

    注意：**不要**在这里注入 Authorization 头。p123client 的客户端对象自身
    已携带当前 token（小写 authorization 键，换票重登后自动更新）；若这里再
    注入一个大写 Authorization 键，会在 httpx 大小写不敏感合并时覆盖客户端
    的键，导致 token 过期后 p123client 自动重登并重试时仍携带旧 token，
    401 直接暴露给插件（按需分享搜索定位被拒）。
    """
    return {
        "User-Agent": user_agent or DEFAULT_UA,
        "platform": WEB_PLATFORM,
        "App-Version": WEB_APP_VERSION,
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
    login_msgs = []
    for style in _TICKET_HEADER_STYLES:
        headers = {
            "User-Agent": user_agent or DEFAULT_UA,
            "Authorization": f"Bearer {token}" if token else "",
        }
        headers.update(style)
        try:
            resp = requests.post(
                f"{SHARE_API_BASE}/api/share/download/info",
                json=payload,
                headers=headers,
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
        if code in (0, None):
            break  # 换票成功
        if code == 5112 or "登录" in message:
            # 登录态校验失败：换下一平台模板重试（token 体系与平台头不匹配）
            login_msgs.append(f"{style.get('platform')}: {message}")
            logger.debug(
                f"【123多盘】分享票换票需登录（platform={style.get('platform')}），尝试下一模板"
            )
            continue
        # 明确错误码日志（验收标准 3：5112/400/50002 需可辨识）
        if code == 400 or "源文件不存在" in message:
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
    else:
        # 所有平台模板均被要求登录
        logger.error(
            "【123多盘】分享下载要求登录态（code=5112 "
            f"{'；'.join(login_msgs)}"
            "），已尝试 web/open_platform/android 三种平台模板均被拒绝，"
            "请检查插件账号登录状态（123 大文件分享下载必须携带有效登录态）"
        )
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


# ==================== 按需分享（懒建分享）模式 ====================
#
# 玩法：播放时按 md5+size 在账号网盘内定位【原文件】，自动创建一个只含该
# 文件的分享（带有效期，到期由 123 自动回收），再用分享票播放。
# - 不预建目录分享、不索引全目录：任何自己上传/转存的文件都能直接播放
# - 同一文件在有效期内多次播放只建一次分享，分享到期后下次播放自动重建
# - 分享带提取码可选（默认免密，播放链路不需要用户交互）
# - 定位：GET {base}/api/file/list/new（SearchData 全局按名搜索）优先，未命中
#   再递归 fs_list 遍历兜底；只按 etag+size 精确命中原文件并返回其
#   FileId/S3KeyFlag，纯查询、不创建任何副本。不采用 upload_request 秒传定位
#   （v1.4.7）：秒传会在根目录生成重复文件（虚拟转存），且同名文件已存在时
#   服务端返回 5060 直接中断播放

ON_DEMAND_API_BASE = "https://api.123278.com/b"
_MAX_ON_DEMAND_CACHE = 128
# 全局搜索最多翻页数（每页 100 条）：单文件命中通常在第 1 页
_MAX_SEARCH_PAGES = 5
# 遍历兜底上限（防超大网盘拖垮首次播放）：超过即放弃并提示用户
_MAX_WALK_DIRS = 500
_MAX_WALK_ITEMS = 20000


def on_demand_expire_iso(days: int) -> str:
    """按需分享有效期：now + N 天，123 要求 ISO8601 +08:00 格式"""
    dt = datetime.now() + timedelta(days=max(1, int(days)))
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


class OnDemandShareCache:
    """按需分享元数据缓存：key=(disk_name, etag, size) -> 分享记录

    记录含 share_id/share_key/share_pwd/file_id/s3_key_flag/expired_at；
    分享到期后条目不再命中（take 会取回旧记录供调用方清理旧分享）。
    """

    def __init__(self, maxsize: int = _MAX_ON_DEMAND_CACHE):
        self._lock = threading.Lock()
        self._items: Dict[tuple, dict] = {}
        self._maxsize = maxsize

    def take(self, key: tuple) -> Optional[dict]:
        """原子取走记录（无论是否过期），供调用方复用或清理"""
        with self._lock:
            return self._items.pop(key, None)

    def set(self, key: tuple, rec: dict):
        with self._lock:
            if len(self._items) >= self._maxsize:
                # 逐出过期最早的记录
                oldest = None
                for k, v in self._items.items():
                    if (
                        oldest is None
                        or v.get("expired_at", 0) < oldest[1].get("expired_at", 0)
                    ):
                        oldest = (k, v)
                if oldest:
                    self._items.pop(oldest[0], None)
                else:
                    self._items.pop(next(iter(self._items)), None)
            self._items[key] = rec

    def size(self) -> int:
        with self._lock:
            return len(self._items)


def _search_file(
    client,
    name: str,
    md5: str,
    size,
    user_agent: str = "",
    base_url: str = ON_DEMAND_API_BASE,
):
    """全局搜索定位：GET file/list/new（SearchData 按文件名搜索，跨整个网盘）

    文件名仅作搜索关键字，结果按 etag+size 精确命中才采用（避免同名不同内容
    的文件被误定位）；命中返回 (原文件 file_id, s3_key_flag)。纯查询，
    不会在网盘创建任何副本。
    返回 (located, rejected)：命中时 located 为 (file_id, s3_key_flag) 元组，
    否则为 None；rejected 表示请求被接口拒绝（401 登录态失效），调用方据此
    区分「未命中」与「被拒绝」。
    """
    keyword = str(name or "").strip()
    if not keyword:
        return None, False
    if len(keyword) > 128:
        # 超长文件名截断为无扩展名主干（搜索关键字过长可能被服务端拒绝）
        stem = keyword.rsplit(".", 1)[0] if "." in keyword else keyword
        keyword = stem[:128]
    for page in range(1, _MAX_SEARCH_PAGES + 1):
        try:
            resp = client.fs_list_new(
                {
                    "SearchData": keyword,
                    "Page": page,
                    "limit": 100,
                    "operateType": 2,
                    "fileCategory": 0,
                },
                base_url=base_url,
                headers=_web_headers(user_agent),
            )
            check_response(resp)
        except P123AuthenticationError as e:
            # 附 token 形态便于诊断：123 网关对非 JWT 格式的 Bearer 值返回
            # 「token contains an invalid number of segments」（正常 JWT 为
            # 三段式），长度+段数一眼可辨是过期重登问题还是 token 已损坏。
            live_token = getattr(client, "token", "") or ""
            logger.warn(
                f"【123多盘】按需分享搜索定位请求被拒绝（账号登录态失效或接口 401）：{e}"
                f"（当前 token 长度 {len(live_token)}，段数 {live_token.count('.')}）"
            )
            return None, True
        except Exception as e:
            logger.warn(f"【123多盘】按需分享搜索定位请求失败: {e}")
            return None, False
        data = resp.get("data") or {}
        items = data.get("InfoList") or data.get("infoList") or []
        for it in items:
            if str(it.get("Type", 0)) in ("1", "True"):
                continue  # 目录
            if int(it.get("Size") or 0) != int(size or 0):
                continue
            if str(it.get("Etag") or "").lower() != str(md5 or "").lower():
                continue
            file_id = it.get("FileId") or it.get("fileId") or it.get("FileID")
            if not file_id:
                continue
            return (int(file_id), str(it.get("S3KeyFlag") or "")), False
        if not items or str(data.get("Next") or "") == "-1":
            break
    return None, False


def _walk_find_file(client, md5: str, size):
    """遍历兜底定位：BFS 递归 fs_list，按 etag+size 精确匹配原文件

    仅当全局搜索未命中时使用（如文件已被改名，搜索不到）；纯查询，
    不创建任何副本。带目录/条目上限防止超大网盘拖垮首次播放。
    """
    size = int(size or 0)
    visited = set()
    queue = [0]
    dirs_seen = 0
    items_seen = 0
    while queue:
        parent = queue.pop(0)
        if str(parent) in visited:
            continue
        visited.add(str(parent))
        page = 1
        while True:
            try:
                resp = client.fs_list(
                    {
                        "limit": 100,
                        "next": 0,
                        "Page": page,
                        "parentFileId": int(parent),
                        "inDirectSpace": "false",
                    }
                )
                check_response(resp)
            except P123AuthenticationError as e:
                live_token = getattr(client, "token", "") or ""
                logger.warn(
                    f"【123多盘】按需分享遍历定位请求被拒绝（账号登录态失效或接口 401）：{e}"
                    f"（当前 token 长度 {len(live_token)}，段数 {live_token.count('.')}）"
                )
                return None
            except Exception as e:
                logger.warn(f"【123多盘】按需分享遍历定位请求失败: {e}")
                return None
            data = resp.get("data") or {}
            items = data.get("InfoList") or data.get("infoList") or []
            if not items:
                break
            for it in items:
                items_seen += 1
                if items_seen > _MAX_WALK_ITEMS:
                    logger.warn(
                        f"【123多盘】按需分享遍历定位超过 {_MAX_WALK_ITEMS} 项仍未命中，放弃"
                    )
                    return None
                if str(it.get("Type", 0)) in ("1", "True"):
                    queue.append(it.get("FileId") or it.get("fileId") or 0)
                    continue
                if int(it.get("Size") or 0) != size:
                    continue
                if str(it.get("Etag") or "").lower() != str(md5 or "").lower():
                    continue
                file_id = it.get("FileId") or it.get("fileId") or it.get("FileID")
                if not file_id:
                    continue
                return int(file_id), str(it.get("S3KeyFlag") or "")
            if len(items) < 100 or str(data.get("Next") or "") == "-1":
                break
            page += 1
        dirs_seen += 1
        if dirs_seen > _MAX_WALK_DIRS:
            logger.warn(
                f"【123多盘】按需分享遍历定位超过 {_MAX_WALK_DIRS} 个目录仍未命中，放弃"
            )
            return None
    return None


def _locate_file(
    client,
    disk_name: str = "",
    name: str = "",
    md5: str = "",
    size=0,
    user_agent: str = "",
    base_url: str = ON_DEMAND_API_BASE,
):
    """定位网盘内原文件：优先全局搜索，未命中再递归遍历兜底

    两者均为纯查询（不调用秒传、不创建任何副本），返回原文件
    (file_id, s3_key_flag)；找不到返回 None。
    """
    located, rejected = _search_file(client, name, md5, size, user_agent, base_url)
    if located:
        return located
    if rejected:
        logger.warn(
            f"【123多盘】按需分享全局搜索被拒绝（401 登录态失效），直接遍历网盘定位"
            "（仅按需，可能较慢）"
        )
    else:
        logger.warn(
            f"【123多盘】按需分享全局搜索未命中（{name or md5}），改为遍历网盘定位（仅按需，可能较慢）"
        )
    return _walk_find_file(client, md5, size)


def _create_share(
    client,
    file_id,
    name: str,
    ttl_days: int,
    share_pwd: str,
    base_url: str = ON_DEMAND_API_BASE,
):
    """按需建分享：只含单个文件，带有效期；返回 {share_id, share_key, share_pwd}"""
    try:
        resp = client.share_create(
            {
                "fileIdList": str(int(file_id)),
                "shareName": (name or "")[:30] or "按需分享",
                "sharePwd": share_pwd or "",
                "fillPwdSwitch": 1 if share_pwd else 0,
                "expiration": on_demand_expire_iso(ttl_days),
                "displayStatus": 2,
                "driveId": 0,
                "event": "shareCreate",
                "isPayShare": False,
                "isReward": 0,
                "payAmount": 0,
                "renameVisible": False,
                "resourceDesc": "",
                "trafficLimit": 0,
                "trafficLimitSwitch": 1,
                "trafficSwitch": 1,
            },
            base_url=base_url,
            async_=False,
        )
        check_response(resp)
    except Exception as e:
        logger.error(f"【123多盘】按需分享建分享请求失败: {e}")
        return None
    data = resp.get("data") or {}
    share_id = data.get("ShareId") or data.get("shareId")
    share_key = data.get("ShareKey") or data.get("shareKey")
    if not share_key:
        logger.error(f"【123多盘】按需分享建分享响应缺少 ShareKey: {resp}")
        return None
    pwd = data.get("SharePwd") or data.get("sharePwd") or share_pwd or ""
    return {"share_id": str(share_id or ""), "share_key": share_key, "share_pwd": pwd}


def _cancel_share(client, share_id, base_url: str = ON_DEMAND_API_BASE):
    """best-effort 取消分享（旧分享到期/失效时清理）"""
    if not share_id:
        return
    try:
        client.share_cancel(str(share_id), base_url=base_url, async_=False)
        logger.debug(f"【123多盘】已清理按需分享 {share_id}")
    except Exception as e:
        logger.debug(f"【123多盘】按需分享清理旧分享失败（忽略）: {e}")


def get_on_demand_share_url(
    account,
    disk_name: str,
    name: str,
    md5: str,
    size,
    ttl_days: int = 7,
    share_pwd: str = "",
    user_agent: str = "",
    ticket_cache: Optional[ShareTicketCache] = None,
    share_cache: Optional[OnDemandShareCache] = None,
    timeout: float = 8,
) -> Optional[str]:
    """按需分享票完整链路（懒建分享）

    1. 缓存命中且分享未过期 -> 复用该分享换票播放（同一文件有效期内只建一次分享）
    2. 分享已过期 -> 先取消旧分享（到期自清理），再建新分享
    3. 换票/解析失败（分享被风控或失效）-> 取消旧分享重建，重试一次
    4. 定位/建分享/换票失败均返回 None，日志明确原因
    :return: 可直接 302 给 Emby 的最终边缘 URL；失败 None
    """
    if not md5 or not size:
        logger.error("【123多盘】按需分享需要文件的 md5 与 size（STRM URL 参数缺失）")
        return None
    token = getattr(account.client, "token", "") or ""
    if not token:
        logger.error("【123多盘】按需分享换票缺少登录态（账号未登录）")
        return None
    key = (disk_name, md5, int(size or 0))
    rec = share_cache.take(key) if share_cache else None
    if rec:
        if rec.get("expired_at", 0) <= time.time():
            _cancel_share(account.client, rec.get("share_id"))  # 到期自动清理
            rec = None
        else:
            url = get_share_download_url(
                token,
                rec["share_key"],
                rec.get("share_pwd") or share_pwd,
                rec["file_id"],
                rec.get("s3_key_flag", ""),
                md5,
                size,
                user_agent=user_agent,
                cache=ticket_cache,
                timeout=timeout,
            )
            if url:
                if share_cache:
                    share_cache.set(key, rec)
                return url
            # 换票/解析失败：分享可能被风控或失效 -> 重建
            logger.warn(
                f"【123多盘】按需分享 {name} 换票失败，取消旧分享后重建"
            )
            _cancel_share(account.client, rec.get("share_id"))
            rec = None
    # 建新分享（定位原文件：全局搜索优先、遍历兜底，纯查询不创建副本）
    located = _locate_file(
        account.client,
        disk_name=disk_name,
        name=name,
        md5=md5,
        size=size,
        user_agent=user_agent,
    )
    if not located:
        logger.error(
            f"【123多盘】按需分享定位文件失败（{name}，md5={md5}，size={size}）："
            "已尝试全局搜索与遍历，网盘中未找到原文件，请确认 STRM 与网盘数据一致"
            "（文件名/大小/md5），或切换回 VIP 直链模式"
        )
        return None
    file_id, s3_flag = located
    share = _create_share(account.client, file_id, name, ttl_days, share_pwd)
    if not share:
        logger.error(
            f"【123多盘】按需分享建分享失败（{name}），请稍后重试或切换 VIP 直链模式"
        )
        return None
    logger.info(
        f"【123多盘】已为 {name} 创建按需分享（有效期 {ttl_days} 天，"
        f"{on_demand_expire_iso(ttl_days)} 过期，提取码{'有' if share['share_pwd'] else '无'}）"
    )
    rec = {
        "share_id": share["share_id"],
        "share_key": share["share_key"],
        "share_pwd": share["share_pwd"],
        "file_id": file_id,
        "s3_key_flag": s3_flag,
        "expired_at": time.time() + max(1, int(ttl_days)) * 86400,
    }
    url = get_share_download_url(
        token,
        rec["share_key"],
        rec["share_pwd"],
        file_id,
        s3_flag,
        md5,
        size,
        user_agent=user_agent,
        cache=ticket_cache,
        timeout=timeout,
    )
    if url and share_cache:
        share_cache.set(key, rec)
    return url
