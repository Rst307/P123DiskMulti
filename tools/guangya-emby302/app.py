from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

from aiohttp import ClientSession, ClientTimeout, TCPConnector, WSMsgType, web


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("guangya-emby302")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on", "y"}


def clean_origin(value: str) -> str:
    return (value or "").strip().rstrip("/")


EMBY_ORIGIN = clean_origin(os.getenv("EMBY_ORIGIN", "http://emby:8096"))
MOVIEPILOT_ORIGIN = clean_origin(os.getenv("MOVIEPILOT_ORIGIN", "http://moviepilot:3000"))
EMBY_API_PREFIX = "/" + os.getenv("EMBY_API_PREFIX", "emby").strip("/")
if EMBY_API_PREFIX == "/":
    EMBY_API_PREFIX = ""
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "").strip()

# Emby 看到的 STRM 根目录与本服务容器内挂载目录。
EMBY_STRM_PREFIX = os.getenv("EMBY_STRM_PREFIX", "/strm").rstrip("/") or "/"
LOCAL_STRM_PREFIX = os.getenv("LOCAL_STRM_PREFIX", "/strm").rstrip("/") or "/"

# 默认只对经过 Cloudflare 的外网请求做 302，局域网访问保持原 Emby 行为。
ONLY_CF = env_bool("ONLY_CF", True)
ALLOW_HTTP_DIRECT = env_bool("ALLOW_HTTP_DIRECT", False)
REQUIRE_STATIC = env_bool("REQUIRE_STATIC", False)
DIRECT_CACHE_TTL = max(0, int(os.getenv("DIRECT_CACHE_TTL", "15") or "15"))
PORT = int(os.getenv("PORT", "8091") or "8091")

# 只拦截 Emby 直接视频流端点。master.m3u8 等转码/HLS 端点不会命中。
VIDEO_RE = re.compile(
    r"^/(?:emby/)?Videos?/(?P<item_id>[^/]+)/(?:"
    r"stream(?:\.[^/?]+)?|original(?:\.[^/?]+)?"
    r")$",
    re.IGNORECASE,
)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

TRANSCODE_HINTS = {
    "videocodec",
    "audiocodec",
    "videobitrate",
    "audiobitrate",
    "maxwidth",
    "maxheight",
    "maxvideobitrate",
    "transcodingmaxaudiochannels",
    "subtitlemethod",
}

_session: Optional[ClientSession] = None
_direct_cache: dict[str, tuple[float, str]] = {}


def request_token(request: web.Request) -> str:
    """优先复用客户端自己的 Emby token，避免必须配置管理员 API Key。"""
    for header_name in ("X-Emby-Token", "X-MediaBrowser-Token"):
        value = request.headers.get(header_name)
        if value:
            return value.strip()

    for key in ("api_key", "apikey", "token"):
        value = request.query.get(key)
        if value:
            return value.strip()

    authorization = request.headers.get("Authorization", "")
    match = re.search(r'(?:Token|token)="?([^",\s]+)', authorization)
    if match:
        return match.group(1)

    return EMBY_API_KEY


def is_cloudflare_request(request: web.Request) -> bool:
    return bool(request.headers.get("CF-Connecting-IP"))


def looks_like_transcode(request: web.Request) -> bool:
    """
    明显需要转码时回退给 Emby。
    Direct Play / static=true 才最适合直接跳到光鸭原文件。
    """
    static_value = (request.query.get("Static") or request.query.get("static") or "").lower()
    if static_value == "true":
        return False
    if request.path.lower().split("/")[-1].startswith("original"):
        return False
    if REQUIRE_STATIC:
        return True
    query_keys = {str(key).lower() for key in request.query.keys()}
    return bool(query_keys & TRANSCODE_HINTS)


def proxy_headers(request: web.Request, *, websocket: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower == "host" or lower in HOP_BY_HOP:
            continue
        if websocket and lower.startswith("sec-websocket-"):
            continue
        headers[key] = value

    if request.host:
        headers.setdefault("X-Forwarded-Host", request.host)
    headers.setdefault(
        "X-Forwarded-Proto",
        request.headers.get("X-Forwarded-Proto", request.scheme),
    )

    remote = request.headers.get("CF-Connecting-IP") or request.remote
    if remote:
        current = headers.get("X-Forwarded-For")
        headers["X-Forwarded-For"] = f"{current}, {remote}" if current else remote
    return headers


def response_headers(headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP
    }


def candidate_playback_urls(item_id: str) -> list[str]:
    suffix = f"/Items/{item_id}/PlaybackInfo"
    urls: list[str] = []
    if EMBY_API_PREFIX:
        urls.append(f"{EMBY_ORIGIN}{EMBY_API_PREFIX}{suffix}")
    urls.append(f"{EMBY_ORIGIN}{suffix}")
    return list(dict.fromkeys(urls))


async def get_media_path(request: web.Request, item_id: str) -> Optional[str]:
    token = request_token(request)
    if not token:
        log.warning("302 skipped: no Emby token item=%s", item_id)
        return None

    params = {"api_key": token}
    user_id = request.query.get("UserId") or request.query.get("userId")
    if user_id:
        params["UserId"] = user_id

    requested_source = (
        request.query.get("MediaSourceId")
        or request.query.get("mediaSourceId")
        or request.query.get("mediasourceid")
    )

    assert _session is not None
    for url in candidate_playback_urls(item_id):
        try:
            async with _session.get(
                url,
                params=params,
                headers={"X-Emby-Token": token},
                allow_redirects=False,
            ) as response:
                if response.status == 404:
                    continue
                if response.status >= 400:
                    log.warning(
                        "PlaybackInfo failed item=%s status=%s",
                        item_id,
                        response.status,
                    )
                    return None
                payload = await response.json(content_type=None)
        except Exception as exc:
            log.warning("PlaybackInfo error item=%s: %s", item_id, exc)
            continue

        sources = payload.get("MediaSources") or []
        if not sources:
            return None

        selected = None
        if requested_source:
            selected = next(
                (
                    source
                    for source in sources
                    if str(source.get("Id") or "") == str(requested_source)
                ),
                None,
            )
        selected = selected or sources[0]
        return str(selected.get("Path") or "").strip() or None

    return None


def map_strm_path(emby_path: str) -> Optional[Path]:
    normalized = emby_path.replace("\\", "/")
    prefix = EMBY_STRM_PREFIX.replace("\\", "/").rstrip("/") or "/"

    if prefix != "/":
        if normalized != prefix and not normalized.startswith(prefix + "/"):
            return None
        relative = normalized[len(prefix):].lstrip("/")
    else:
        relative = normalized.lstrip("/")

    local_root = Path(LOCAL_STRM_PREFIX).expanduser().resolve(strict=False)
    local_path = local_root.joinpath(*Path(relative).parts).resolve(strict=False)
    if local_path != local_root and local_root not in local_path.parents:
        return None
    return local_path


def read_strm_target(media_path: str) -> Optional[str]:
    # 部分 Emby 版本的 PlaybackInfo 对 STRM 会直接返回远程 URL。
    if media_path.startswith(("http://", "https://")):
        return media_path

    if not media_path.lower().endswith(".strm"):
        return None

    local_path = map_strm_path(media_path)
    if not local_path or not local_path.is_file():
        log.warning("STRM not visible: %s", media_path)
        return None

    try:
        # STRM 理论上只有一行；限制最大读取量，避免配置错误误读大文件。
        data = local_path.read_text(encoding="utf-8", errors="replace")[: 16 * 1024]
    except OSError as exc:
        log.warning("Read STRM failed %s: %s", local_path, exc)
        return None

    return next(
        (
            line.strip()
            for line in data.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ),
        None,
    )


def allowed_direct_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and ALLOW_HTTP_DIRECT


async def resolve_guangya_url(strm_target: str) -> Optional[str]:
    """
    STRM 内保存的是 GuangYaStrm /play 地址。
    本服务只在内网访问该地址一次，取得其 302 Location 后再直接回给外网客户端。
    """
    try:
        parsed = urlsplit(strm_target)
    except ValueError:
        return None

    if "/api/v1/plugin/GuangYaStrm/play" not in parsed.path:
        return None

    now = time.monotonic()
    cached = _direct_cache.get(strm_target)
    if cached and cached[0] > now:
        return cached[1]

    internal_url = f"{MOVIEPILOT_ORIGIN}{parsed.path}"
    if parsed.query:
        internal_url += "?" + parsed.query

    assert _session is not None
    try:
        async with _session.get(internal_url, allow_redirects=False) as response:
            if response.status not in {301, 302, 303, 307, 308}:
                log.warning(
                    "MoviePilot resolver status=%s",
                    response.status,
                )
                return None
            location = response.headers.get("Location")
    except Exception as exc:
        log.warning("MoviePilot resolver error: %s", exc)
        return None

    if not location:
        return None

    location = urljoin(internal_url, location)
    if not allowed_direct_url(location):
        log.warning(
            "Direct URL rejected scheme=%s",
            urlsplit(location).scheme,
        )
        return None

    if DIRECT_CACHE_TTL > 0:
        _direct_cache[strm_target] = (now + DIRECT_CACHE_TTL, location)
    return location


async def try_direct_redirect(request: web.Request) -> None:
    if request.method not in {"GET", "HEAD"}:
        return

    match = VIDEO_RE.match(request.path)
    if not match:
        return

    if ONLY_CF and not is_cloudflare_request(request):
        return

    # 转码仍交给 Emby，只有直放/直流走光鸭 302。
    if looks_like_transcode(request):
        return

    item_id = match.group("item_id")
    media_path = await get_media_path(request, item_id)
    if not media_path:
        return

    strm_target = read_strm_target(media_path)
    if not strm_target:
        return

    direct_url = await resolve_guangya_url(strm_target)
    if not direct_url:
        return

    log.info(
        "302 direct item=%s client=%s target_host=%s",
        item_id,
        request.headers.get("CF-Connecting-IP") or request.remote,
        urlsplit(direct_url).netloc,
    )
    raise web.HTTPFound(
        direct_url,
        headers={
            "Cache-Control": "no-store",
            "X-GuangYa-302": "direct",
        },
    )


async def proxy_http(request: web.Request) -> web.StreamResponse:
    """普通 Emby 请求保持反代；视频只有未命中直链时才会走这里。"""
    assert _session is not None

    target = EMBY_ORIGIN + request.rel_url.path_qs
    body = request.content.iter_chunked(256 * 1024) if request.can_read_body else None

    upstream = await _session.request(
        request.method,
        target,
        headers=proxy_headers(request),
        data=body,
        allow_redirects=False,
    )

    response = web.StreamResponse(
        status=upstream.status,
        reason=upstream.reason,
        headers=response_headers(upstream.headers),
    )
    await response.prepare(request)

    if request.method != "HEAD":
        try:
            async for chunk in upstream.content.iter_chunked(256 * 1024):
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    upstream.release()
    try:
        await response.write_eof()
    except (ConnectionResetError, RuntimeError):
        pass
    return response


async def proxy_websocket(request: web.Request) -> web.WebSocketResponse:
    """保持 Emby WebSocket 可用。"""
    assert _session is not None

    origin = urlsplit(EMBY_ORIGIN)
    websocket_scheme = "wss" if origin.scheme == "https" else "ws"
    target = (
        f"{websocket_scheme}://{origin.netloc}"
        f"{request.rel_url.path_qs}"
    )

    downstream = web.WebSocketResponse(autoping=True, heartbeat=30)
    await downstream.prepare(request)

    try:
        upstream = await _session.ws_connect(
            target,
            headers=proxy_headers(request, websocket=True),
            autoping=True,
            heartbeat=30,
        )
    except Exception as exc:
        log.warning("WebSocket upstream failed: %s", exc)
        await downstream.close(
            code=1011,
            message=b"upstream unavailable",
        )
        return downstream

    async def client_to_upstream():
        async for message in downstream:
            if message.type == WSMsgType.TEXT:
                await upstream.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await upstream.send_bytes(message.data)
            elif message.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                break

    async def upstream_to_client():
        async for message in upstream:
            if message.type == WSMsgType.TEXT:
                await downstream.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await downstream.send_bytes(message.data)
            elif message.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                break

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    _, pending = await asyncio.wait(
        tasks,
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    await upstream.close()
    if not downstream.closed:
        await downstream.close()
    return downstream


async def health(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "mode": "cloudflare-only" if ONLY_CF else "all-clients",
            "emby_origin": EMBY_ORIGIN,
            "moviepilot_origin": MOVIEPILOT_ORIGIN,
            "emby_strm_prefix": EMBY_STRM_PREFIX,
            "local_strm_prefix": LOCAL_STRM_PREFIX,
            "allow_http_direct": ALLOW_HTTP_DIRECT,
        }
    )


async def gateway(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_websocket(request)

    await try_direct_redirect(request)
    return await proxy_http(request)


async def on_startup(_: web.Application):
    global _session
    _session = ClientSession(
        timeout=ClientTimeout(
            total=None,
            sock_connect=15,
            sock_read=None,
        ),
        connector=TCPConnector(
            limit=200,
            ttl_dns_cache=300,
        ),
        auto_decompress=False,
    )
    log.info(
        "GuangYa Emby302 port=%s emby=%s moviepilot=%s only_cf=%s",
        PORT,
        EMBY_ORIGIN,
        MOVIEPILOT_ORIGIN,
        ONLY_CF,
    )


async def on_cleanup(_: web.Application):
    global _session
    if _session is not None:
        await _session.close()
        _session = None


def create_app() -> web.Application:
    app = web.Application(client_max_size=0)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/__guangya302/health", health)
    app.router.add_route("*", "/{tail:.*}", gateway)
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(),
        host="0.0.0.0",
        port=PORT,
        access_log=None,
    )
