"""P123DiskMulti 客户端工具模块

1. P123AutoClient：懒加载 + Token 超限自动重连的 123 客户端包装
2. 下载换链（签票）域名管理：123 存在多个换链通道（域名），各通道独立风控。
   通道被风控时，换链接口本身仍正常返回带签名的新链接，但 CDN 网关下载时
   统一返回 HTTP 403 {"message":"download err: 50002","code":1010}。
   本模块实现「换链 -> 票有效性探测 -> 被风控自动切换域名重试」。

背景（2026-08 全库播放故障实证）：
- 签票身份由换链请求域名决定，URL 中 ur 参数前缀可区分：
  yun.123pan.com/b（p123client 默认，第三方集成官方域名）→ ur=vpagiagaelg* 家族；
  api.123278.com/b（网页前端实际使用的官方网关）→ ur=vpngvaegvngvnp 家族。
- 账号在 yun 通道被风控后全库 403，同一 token/文件仅换域名即可恢复，
  与 IP/UA/token 类型/文件/节点均无关（已对照实验排除）。
"""

import errno as _errno
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

import requests
from p123client import P123Client, check_response

from app.log import logger

# p123client 内部引用了 FreeBSD 专属的 errno.EAUTH（认证错误码，仅 FreeBSD 存在）；
# 在 Linux/Windows/macOS 上运行时，接口返回 401（登录态失效）时本应抛
# P123AuthenticationError，却会抛 AttributeError（module 'errno' has no attribute
# 'EAUTH'），掩盖真实原因。这里补一个可移植等价码（EPERM，语义同为"操作不被允许"），
# 确保认证失败能被正常识别与日志化（对已修复的 p123client 无影响：FreeBSD 上自带
# EAUTH 会跳过补丁，其它平台也只是补一个本不存在的常量）。
if not hasattr(_errno, "EAUTH"):
    try:
        _errno.EAUTH = _errno.EPERM
    except Exception:
        pass  # 极端环境（如只读 site-packages）下忽略，不影响主流程

# 换链（签票）域名候选，按优先级：
# - https://api.123278.com/b：123 网页前端实际使用的网关域名（与 123pan.com 同属
#   西安一二三云计算有限公司，ICP 备案 陕ICP备2021011326号-22），网页端下载一直可用；
# - ""：p123client 默认域名（www.123pan.com/b，实际跳转 yun.123pan.com/b），
#   是 123 官方给第三方挂载应用授权的域名，风控更严格，曾出现整账号全文件 403 50002。
# 两通道签发的下载票身份不同，CDN 按票身份做风控判定，因此通道被风控时切换到
# 另一通道即可恢复。
DEFAULT_DOWNLOAD_BASE_URLS: Tuple[str, ...] = (
    "https://api.123278.com/b",
    "",
)

# 工作域名有效期（秒）：某域名验证通过后优先使用，到期后重新按候选序探测
_WORKING_TTL = 30 * 60
# 失败域名冷却（秒）：冷却期内的域名排到最后，避免每次请求都重复踩被风控的通道
_FAIL_COOLDOWN = 5 * 60
# 票有效性探测超时（秒）
_DEFAULT_PROBE_TIMEOUT = 8
# 已验证下载直链缓存（秒）：降低签票频率。Emby 每次播放会 HEAD+GET 两次签票，
# 重缓冲/反复点播更频繁；风控大概率由签票频率/模式触发，缓存命中即不再签票，
# 能把签票频率降一个量级（123 直链有效期远大于缓存 TTL：否则长片播放中早已断流）
DEFAULT_URL_CACHE_TTL = 600
# 缓存条目上限（防内存无界增长）
_URL_CACHE_MAX = 256


def _label(base: str) -> str:
    """域名日志标签：空串表示 p123client 默认域名"""
    return base or "默认(www.123pan.com/b)"


class DownloadTicketState:
    """
    单个账号的换链域名状态（工作域名 + 失败冷却）

    状态保存在 P123AutoClient 上，按账号隔离：A 账号在 yun 通道被风控，
    不影响 B 账号继续优先使用 yun 通道。
    """

    def __init__(self, base_urls: Optional[Iterable[str]] = None):
        self.base_urls: Tuple[str, ...] = tuple(
            base_urls or DEFAULT_DOWNLOAD_BASE_URLS
        )
        self.working: Optional[str] = None  # 最近验证通过的域名
        self.working_until: float = 0.0  # working 状态到期时间
        self.failed: Dict[str, float] = {}  # 域名 -> 最近失败时间
        # 已验证下载直链缓存：文件标识 -> (url, 过期时间戳)
        self._url_cache: Dict[tuple, Tuple[str, float]] = {}
        self._lock = threading.Lock()

    def candidates(self) -> List[str]:
        """当前尝试顺序：工作域名优先，冷却中的域名排最后（仍会兜底尝试）"""
        now = time.time()
        with self._lock:
            hot = {
                d for d, t in self.failed.items() if now - t < _FAIL_COOLDOWN
            }
            order: List[str] = []
            if (
                self.working
                and now < self.working_until
                and self.working not in hot
            ):
                order.append(self.working)
            for b in self.base_urls:
                if b not in order:
                    order.append(b)
        return [b for b in order if b not in hot] + [b for b in order if b in hot]

    def mark_working(self, base: str):
        """标记某域名换票验证通过"""
        now = time.time()
        with self._lock:
            self.working = base
            self.working_until = now + _WORKING_TTL
            self.failed.pop(base, None)

    def mark_failed(self, base: str):
        """标记某域名换票失败（进入冷却）"""
        with self._lock:
            self.failed[base] = time.time()

    def cache_get(self, key: tuple) -> Optional[str]:
        """取未过期的已验证直链缓存"""
        if not key:
            return None
        now = time.time()
        with self._lock:
            item = self._url_cache.get(key)
            if item and item[1] > now:
                return item[0]
            if item:
                self._url_cache.pop(key, None)
        return None

    def cache_set(self, key: tuple, url: str, ttl: float):
        """写入已验证直链缓存（ttl<=0 不缓存）"""
        if not key or ttl <= 0:
            return
        with self._lock:
            if len(self._url_cache) >= _URL_CACHE_MAX:
                # 清理过期条目，仍满则丢弃最旧的一条
                now = time.time()
                stale = [k for k, (_, exp) in self._url_cache.items() if exp <= now]
                for k in stale:
                    self._url_cache.pop(k, None)
                if len(self._url_cache) >= _URL_CACHE_MAX:
                    oldest = min(
                        self._url_cache.items(), key=lambda kv: kv[1][1]
                    )
                    self._url_cache.pop(oldest[0], None)
            self._url_cache[key] = (url, time.time() + ttl)


def _cache_key(payload: dict) -> tuple:
    """按文件全局标识构造缓存键（S3KeyFlag 是 123 全局文件标识）"""
    return tuple(
        payload.get(k)
        for k in ("S3KeyFlag", "Etag", "FileID", "Size")
        if payload.get(k) is not None
    ) or ()


def probe_download_url(
    url: str,
    headers: Optional[dict] = None,
    timeout: float = _DEFAULT_PROBE_TIMEOUT,
) -> Tuple[bool, int, str]:
    """
    探测下载直链票的有效性（Range GET 首字节，立即断开）

    :return: (ok, status, detail)
        - ok=True  status=200/206 票有效
        - ok=False status=403     票被 CDN 网关拒绝（风控/地域绑定），detail 为解析出的内容
        - ok=False status=0       网络异常无法判定（探测方与下载方网络路径可能不同）
        - ok=False 其他 status    服务器明确拒绝但非风控（404/5xx 等）
    """
    req_headers = {"Range": "bytes=0-0", "Connection": "close"}
    if headers:
        req_headers.update(
            {k: v for k, v in headers.items() if k.lower() != "range"}
        )
    try:
        resp = requests.get(
            url,
            headers=req_headers,
            stream=True,
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return False, 0, f"{type(e).__name__}: {e}"
    try:
        if resp.status_code in (200, 206):
            return True, resp.status_code, ""
        detail = ""
        try:
            body = resp.json()
            detail = (
                f"message={body.get('message') or ''} "
                f"code={body.get('code') or ''}"
            ).strip()
        except Exception:
            detail = (resp.text or "")[:200]
        return False, resp.status_code, detail
    except Exception as e:
        # 响应解析异常（如非标准响应）：视为无法判定，避免探测破坏播放
        return False, 0, f"探测响应异常: {e}"
    finally:
        try:
            resp.close()
        except Exception:
            pass


def exchange_and_validate(
    client,
    payload: dict,
    headers: Optional[dict] = None,
    state: Optional[DownloadTicketState] = None,
    probe: Optional[Union[Callable, bool]] = None,
    timeout: float = _DEFAULT_PROBE_TIMEOUT,
    cache_ttl: float = DEFAULT_URL_CACHE_TTL,
) -> Optional[str]:
    """
    换取 123 下载直链并验证票有效性；通道被风控时自动切换域名重试

    :param client: 123 客户端（具备 download_info(payload, base_url=..., async_=False, headers=...)）
    :param payload: download_info 载荷（Etag/S3KeyFlag/FileName/Size 等）
    :param headers: 换链请求头（User-Agent 等）
    :param state: 每账号域名状态；None 时新建
    :param probe: 探测函数 (url, headers, timeout) -> (ok, status, detail)；
                  None 用默认探测；False 表示关闭探测（取第一个换链成功结果）
    :param timeout: 探测超时（秒）
    :param cache_ttl: 已验证直链缓存秒数；<=0 关闭缓存。缓存命中即不再签票，
                      能显著降低签票频率（风控触发的主要信号）
    :return: 验证通过的 DownloadUrl；全部通道失败返回 None
    """
    state = state or DownloadTicketState(None)
    key = _cache_key(payload)
    if cache_ttl > 0 and key:
        cached = state.cache_get(key)
        if cached:
            logger.debug(
                f"【123多盘】命中已验证下载链接缓存（避免重复签票）: {cached[:200]}"
            )
            return cached
    candidates = state.candidates()
    if probe is False:
        # 探测关闭：按候选顺序取第一个换链成功的结果（不做有效性验证，兼容旧行为）
        for base in candidates:
            try:
                resp = client.download_info(
                    payload, base_url=base, async_=False, headers=headers
                )
                check_response(resp)
                url = (resp.get("data") or {}).get("DownloadUrl")
                if url:
                    state.mark_working(base)
                    state.cache_set(key, url, cache_ttl)
                    return url
            except Exception as e:
                state.mark_failed(base)
                logger.warn(f"【123多盘】换链失败（{_label(base)}）: {e}")
        return None
    probe_fn = probe or probe_download_url
    last_err = ""
    for base in candidates:
        label = _label(base)
        try:
            resp = client.download_info(
                payload, base_url=base, async_=False, headers=headers
            )
            check_response(resp)
            url = (resp.get("data") or {}).get("DownloadUrl")
            if not url:
                raise ValueError("返回数据缺少 DownloadUrl")
        except Exception as e:
            state.mark_failed(base)
            last_err = f"换链失败（{label}）: {e}"
            logger.warn(f"【123多盘】{last_err}")
            continue
        ok, status, detail = probe_fn(url, headers=headers, timeout=timeout)
        if ok:
            state.mark_working(base)
            state.cache_set(key, url, cache_ttl)
            if base != candidates[0]:
                logger.warn(
                    f"【123多盘】下载通道被风控，切换换链域名 {label} 后恢复"
                )
            logger.debug(f"【123多盘】下载链接验证通过: {url[:200]}")
            return url
        if status == 403:
            state.mark_failed(base)
            if "realc" in detail or "city" in detail:
                logger.error(
                    f"【123多盘】CDN 地域绑定拦截（HTTP 403 {detail}）："
                    f"下载出口 IP 与换链方地域不一致，切换域名通常无法解决，"
                    f"请检查 MoviePilot/Emby 网络出口"
                )
            else:
                logger.error(
                    f"【123多盘】下载通道被风控：HTTP 403（{detail or '无详情'}），"
                    f"正在尝试切换换链域名: {label}"
                )
            last_err = f"下载票被网关拒绝（{label}）: HTTP 403 {detail}"
        else:
            # 非 403 失败（404/5xx）或网络异常（status=0）：不做域名切换。
            # 探测方与下载方网络路径可能不同，按原链接返回由下载方自行尝试
            # （未经验证，不入缓存）。
            logger.warn(
                f"【123多盘】下载票探测异常（{label}）: HTTP {status or '网络错误'} "
                f"{detail}，按原链接返回，以实际下载结果为准"
            )
            return url
    logger.error(
        f"【123多盘】所有换链域名均失败（{len(candidates)} 个）: {last_err}，"
        f"请检查 123 账号状态（如被风控 50002 可尝试更换出口 IP 或联系客服）"
    )
    return None


class P123AutoClient:
    """
    123云盘客户端

    懒实例化；代理调用时自动处理 Token 超限重连；
    附带下载换链域名状态（工作域名/失败冷却），支持通道风控自动切换。
    """

    def __init__(self, passport: str, password: str):
        self._client = None
        self._passport = passport
        self._password = password
        # 下载换链域名状态（按账号隔离）
        self._dl_state = DownloadTicketState(None)

    def __getattr__(self, name):
        if self._client is None:
            self._client = P123Client(self._passport, self._password)

        def wrapped(*args, **kwargs):
            """
            代理调用 P123Client 的方法，自动处理 Token 超限重连

            :param args: 传递给客户端方法的位置参数
            :param kwargs: 传递给客户端方法的关键字参数
            :return: 客户端方法的返回值
            """
            attr = getattr(self._client, name)
            if not callable(attr):
                return attr
            result = attr(*args, **kwargs)
            if (
                isinstance(result, dict)
                and result.get("code") == 401
                and result.get("message") == "tokens number has exceeded the limit"
            ):
                self._client = P123Client(self._passport, self._password)
                attr = getattr(self._client, name)
                if not callable(attr):
                    return attr
                return attr(*args, **kwargs)
            return result

        return wrapped

    def relogin(self) -> str:
        """强制重新登录：重建客户端重新 sign_in，返回新 token

        分享票换票被连续 5112 拒绝（token 被服务端作废）时调用；
        P123Client 构造即登录（passport+password），失败会抛异常由调用方处理。
        """
        self._client = P123Client(self._passport, self._password)
        return getattr(self._client, "token", "") or ""

    def set_download_base_urls(self, base_urls: Optional[Iterable[str]] = None):
        """自定义换链域名候选（按优先级排序；空则恢复内置默认）"""
        self._dl_state.base_urls = tuple(
            base_urls or DEFAULT_DOWNLOAD_BASE_URLS
        )

    def get_download_url(
        self,
        payload: dict,
        headers: Optional[dict] = None,
        base_urls: Optional[Iterable[str]] = None,
        probe: bool = True,
        timeout: float = _DEFAULT_PROBE_TIMEOUT,
        cache_ttl: float = DEFAULT_URL_CACHE_TTL,
    ) -> Optional[str]:
        """
        换取并验证 123 下载直链：通道被风控（CDN 403 50002/1010）时自动切换域名重试；
        已验证直链按文件缓存（cache_ttl 秒），命中不再签票，降低风控触发频率

        :param payload: download_info 载荷
        :param headers: 换链请求头（User-Agent 等）
        :param base_urls: 换链域名候选覆盖（None 沿用当前状态/内置默认）
        :param probe: 是否探测票有效性并自动切换域名（默认开启）
        :param timeout: 探测超时（秒）
        :param cache_ttl: 已验证直链缓存秒数（默认 600，0 关闭）
        :return: 验证通过的 DownloadUrl，失败返回 None
        """
        if base_urls is not None:
            self.set_download_base_urls(base_urls)
        return exchange_and_validate(
            self,
            payload,
            headers=headers,
            state=self._dl_state,
            probe=probe_download_url if probe else False,
            timeout=timeout,
            cache_ttl=cache_ttl,
        )
