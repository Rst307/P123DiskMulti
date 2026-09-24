"""MoviePilot V2 光鸭 STRM：无需本地挂载，直接扫描光鸭远程目录生成 STRM。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode

from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from app.core.plugin import PluginManager
from app.log import logger
from app.plugins import _PluginBase


class GuangYaStrm(_PluginBase):
    plugin_name = "光鸭 STRM"
    plugin_desc = "无需挂载光鸭云盘，直接扫描远程目录生成 STRM，并复用光鸭插件流式播放。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.1"
    plugin_author = "Rst307"
    author_url = "https://github.com/Rst307/P123DiskMulti"
    plugin_config_prefix = "guangyastrm_"
    plugin_order = 35
    auth_level = 1

    _enabled = False
    _onlyonce = False
    _source_plugin_id = "ShukGuangYaDisk"
    _remote_root = "/emby"
    _output_root = "/opt/emby/strm/光鸭云盘"
    _base_url = ""
    _interval_minutes = 10
    _cleanup_stale = True
    _stream_secret = ""
    _video_extensions_raw = ".mkv,.mp4,.avi,.mov,.wmv,.flv,.ts,.m2ts,.webm,.iso,.mpg,.mpeg"

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._source_plugin_id = str(config.get("source_plugin_id") or "ShukGuangYaDisk").strip()
        self._remote_root = self._normalize_remote(config.get("remote_root") or "/emby")
        self._output_root = str(config.get("output_root") or "/opt/emby/strm/光鸭云盘").strip()
        self._base_url = str(config.get("base_url") or "").strip().rstrip("/")
        self._interval_minutes = self._positive_int(config.get("interval_minutes"), 10)
        self._cleanup_stale = bool(config.get("cleanup_stale", True))
        self._video_extensions_raw = str(
            config.get("video_extensions")
            or ".mkv,.mp4,.avi,.mov,.wmv,.flv,.ts,.m2ts,.webm,.iso,.mpg,.mpeg"
        )
        self._stream_secret = str(config.get("stream_secret") or "").strip() or secrets.token_urlsafe(32)
        self._sync_lock = threading.Lock()
        self._last_status = {
            "success": None, "message": "等待同步", "last_sync": None,
            "files": 0, "created": 0, "updated": 0, "deleted": 0,
        }
        if not config.get("stream_secret"):
            self._save_config()

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_remote(value: Any) -> str:
        raw = str(value or "/").replace("\\", "/").strip()
        parts = []
        for part in raw.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise ValueError("远程路径不能包含 ..")
            parts.append(part)
        return "/" + "/".join(parts) if parts else "/"

    def _relative(self, remote_path: str) -> Optional[PurePosixPath]:
        candidate = PurePosixPath(self._normalize_remote(remote_path))
        root = PurePosixPath(self._remote_root)
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return None
        if str(relative) in {"", "."} or any(part in {"", ".", ".."} for part in relative.parts):
            return None
        return relative

    def _extensions(self) -> set:
        result = set()
        for item in self._video_extensions_raw.replace(";", ",").replace("\n", ",").split(","):
            ext = item.strip().casefold()
            if ext:
                result.add(ext if ext.startswith(".") else f".{ext}")
        return result

    def _source_plugin(self) -> Tuple[Optional[Any], Optional[str]]:
        try:
            plugin = (PluginManager().running_plugins or {}).get(self._source_plugin_id)
        except Exception as exc:
            return None, f"读取光鸭插件运行态失败：{exc}"
        if not plugin:
            return None, f"{self._source_plugin_id} 未加载"
        if not callable(getattr(plugin, "browse_path", None)):
            return None, "光鸭插件不提供 browse_path()"
        if not callable(getattr(plugin, "stream_file", None)):
            return None, "光鸭插件不提供 stream_file()"
        return plugin, None

    def _save_config(self, onlyonce: Optional[bool] = None):
        return self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce if onlyonce is None else bool(onlyonce),
            "source_plugin_id": self._source_plugin_id,
            "remote_root": self._remote_root,
            "output_root": self._output_root,
            "base_url": self._base_url,
            "interval_minutes": self._interval_minutes,
            "cleanup_stale": self._cleanup_stale,
            "stream_secret": self._stream_secret,
            "video_extensions": self._video_extensions_raw,
        })

    @property
    def _index_path(self) -> Path:
        return self.get_data_path() / "index.json"

    def _load_index(self) -> Dict[str, str]:
        try:
            if not self._index_path.exists():
                return {}
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("【光鸭 STRM】读取索引失败，将重建：%s", exc)
            return {}

    def _write_index(self, data: Dict[str, str]):
        target = self._index_path
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".index.", suffix=".json", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _safe_output(self, relative: PurePosixPath) -> Path:
        if relative.is_absolute() or any(p in {"", ".", ".."} for p in relative.parts):
            raise ValueError("非法输出路径")
        root = Path(self._output_root).expanduser().resolve(strict=False)
        target = root.joinpath(*relative.parts)
        parent = target.parent.resolve(strict=False)
        if parent != root and root not in parent.parents:
            raise ValueError("输出路径越界")
        return target

    @staticmethod
    def _write_text(path: Path, content: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                if path.read_text(encoding="utf-8") == content:
                    return "unchanged"
            except OSError:
                pass
            state = "updated"
        else:
            state = "created"
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_name, 0o644)
            os.replace(temp_name, path)
            return state
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _sign(self, path: str) -> str:
        return hmac.new(
            self._stream_secret.encode("utf-8"),
            path.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _stream_url(self, remote_path: str) -> str:
        if not self._base_url:
            raise ValueError("请先填写 MoviePilot 地址（Emby 可访问）")
        query = urlencode({"path": remote_path, "sig": self._sign(remote_path)})
        return f"{self._base_url}/api/v1/plugin/{self.__class__.__name__}/play?{query}"

    def _iter_videos(self, items: Iterable[Dict[str, Any]]) -> Iterable[str]:
        extensions = self._extensions()
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            remote_path = self._normalize_remote(item.get("path") or "")
            if self._relative(remote_path) is not None and PurePosixPath(remote_path).suffix.casefold() in extensions:
                yield remote_path

    def _cleanup_empty_parents(self, path: Path):
        root = Path(self._output_root).expanduser().resolve(strict=False)
        current = path.parent
        while current != root and root in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def sync(self) -> Dict[str, Any]:
        if not self._sync_lock.acquire(blocking=False):
            return {"success": False, "message": "已有同步任务正在运行"}

        started = datetime.now().astimezone()
        created = updated = deleted = 0
        try:
            plugin, error = self._source_plugin()
            if not plugin:
                raise RuntimeError(error or "光鸭云盘助手不可用")
            if not self._base_url:
                raise RuntimeError("请先填写 MoviePilot 地址（Emby 可访问）")

            listing = plugin.browse_path(path=self._remote_root, recursion=True)
            if not isinstance(listing, dict):
                raise RuntimeError("光鸭 browse_path() 返回格式异常")
            if listing.get("error"):
                raise RuntimeError(str(listing.get("error")))

            current = {}
            Path(self._output_root).expanduser().mkdir(parents=True, exist_ok=True)
            for remote_path in sorted(set(self._iter_videos(listing.get("items") or []))):
                relative = self._relative(remote_path)
                if relative is None:
                    continue
                relative_strm = relative.with_suffix(".strm")
                state = self._write_text(self._safe_output(relative_strm), self._stream_url(remote_path))
                created += int(state == "created")
                updated += int(state == "updated")
                current[remote_path] = relative_strm.as_posix()

            previous = self._load_index()
            if self._cleanup_stale:
                live_targets = set(current.values())
                for remote_path, relative_strm in previous.items():
                    if remote_path in current or relative_strm in live_targets:
                        continue
                    try:
                        stale = self._safe_output(PurePosixPath(relative_strm))
                    except ValueError:
                        continue
                    if stale.suffix.casefold() != ".strm":
                        continue
                    try:
                        if stale.exists() and stale.is_file():
                            stale.unlink()
                            deleted += 1
                            self._cleanup_empty_parents(stale)
                    except OSError as exc:
                        logger.warning("【光鸭 STRM】删除旧 STRM 失败 %s：%s", stale, exc)

            self._write_index(current)
            now = datetime.now().astimezone()
            self._last_status = {
                "success": True,
                "message": "同步完成",
                "last_sync": now.isoformat(timespec="seconds"),
                "duration_seconds": round((now - started).total_seconds(), 2),
                "files": len(current),
                "created": created,
                "updated": updated,
                "deleted": deleted,
            }
            logger.info("【光鸭 STRM】同步完成：媒体=%s 新建=%s 更新=%s 删除=%s", len(current), created, updated, deleted)
            return dict(self._last_status)
        except Exception as exc:
            now = datetime.now().astimezone()
            logger.error("【光鸭 STRM】同步失败：%s", exc)
            self._last_status = {
                "success": False,
                "message": str(exc),
                "last_sync": now.isoformat(timespec="seconds"),
                "duration_seconds": round((now - started).total_seconds(), 2),
                "files": 0, "created": created, "updated": updated, "deleted": deleted,
            }
            return dict(self._last_status)
        finally:
            self._sync_lock.release()

    def _run_once(self):
        result = self.sync()
        self._onlyonce = False
        self._save_config(onlyonce=False)
        return result

    def play(self, request: Request, path: str = "", sig: str = "") -> Response:
        try:
            remote_path = self._normalize_remote(path)
        except ValueError:
            return JSONResponse({"error": "invalid path"}, status_code=400)

        if not sig or not hmac.compare_digest(sig, self._sign(remote_path)):
            return JSONResponse({"error": "invalid signature"}, status_code=403)
        if self._relative(remote_path) is None:
            return JSONResponse({"error": "path outside configured root"}, status_code=403)
        if PurePosixPath(remote_path).suffix.casefold() not in self._extensions():
            return JSONResponse({"error": "unsupported media extension"}, status_code=400)

        plugin, error = self._source_plugin()
        if not plugin:
            return JSONResponse({"error": error or "source plugin unavailable"}, status_code=503)

        # 不直接调用 ShukGuangYaDisk.stream_file()：
        # 上游 V2 当前会把中文文件名原样写进 Content-Disposition，
        # Starlette 构造响应头时按 latin-1 编码，从而导致中文文件名播放 500。
        # 这里复用光鸭插件的登录态/API 客户端，但自行做一个无中文响应头的流式代理。
        try:
            import requests as http_requests

            api = getattr(plugin, "_guangya_api", None)
            client = getattr(plugin, "_client", None)
            if not api or not client:
                return JSONResponse({"error": "source plugin client unavailable"}, status_code=503)

            file_item = api.get_item(Path(remote_path))
            if not file_item or getattr(file_item, "type", None) != "file":
                return JSONResponse({"error": "file not found"}, status_code=404)

            dl_response = client.get_download_url(file_item.fileid)
            if not isinstance(dl_response, dict):
                return JSONResponse({"error": "invalid download response"}, status_code=502)
            if dl_response.get("msg") != "success" and dl_response.get("code") != 0:
                return JSONResponse(
                    {"error": f"get download url failed: {dl_response.get('msg', 'unknown')}"},
                    status_code=502,
                )

            data = dl_response.get("data") or {}
            download_url = data.get("signedURL") or data.get("downloadUrl")
            if not download_url:
                return JSONResponse({"error": "missing download url"}, status_code=502)

            forward_headers = {
                "User-Agent": request.headers.get(
                    "user-agent",
                    "Mozilla/5.0 (compatible; Emby/1.0; GuangYaStrmProxy)",
                ),
                "Referer": "https://www.guangyupan.com/",
            }
            range_header = request.headers.get("range")
            if range_header:
                forward_headers["Range"] = range_header

            upstream_method = "HEAD" if request.method.upper() == "HEAD" else "GET"
            upstream = http_requests.request(
                upstream_method,
                download_url,
                headers=forward_headers,
                stream=(upstream_method == "GET"),
                timeout=300,
                allow_redirects=True,
            )
            if upstream.status_code >= 400:
                upstream.close()
                return JSONResponse(
                    {"error": f"upstream http {upstream.status_code}"},
                    status_code=upstream.status_code,
                )

            response_headers = {
                "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
                "Cache-Control": "public, max-age=3600",
            }
            for source_name, target_name in (
                ("content-length", "Content-Length"),
                ("content-range", "Content-Range"),
                ("etag", "ETag"),
                ("last-modified", "Last-Modified"),
            ):
                value = upstream.headers.get(source_name)
                if value:
                    response_headers[target_name] = value

            # 中文文件名必须使用 RFC 5987 百分号编码，不能直接塞进 latin-1 响应头。
            filename = getattr(file_item, "name", "") or PurePosixPath(remote_path).name
            if filename:
                response_headers["Content-Disposition"] = (
                    "inline; filename*=UTF-8''" + quote(filename, safe="")
                )

            content_type = upstream.headers.get("content-type", "application/octet-stream")
            if upstream_method == "HEAD":
                upstream.close()
                return Response(
                    status_code=upstream.status_code,
                    headers=response_headers,
                    media_type=content_type,
                )

            def generate():
                try:
                    for chunk in upstream.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            yield chunk
                except Exception as exc:
                    logger.warning("【光鸭 STRM】流式传输中断: %s", exc)
                finally:
                    upstream.close()

            return StreamingResponse(
                generate(),
                status_code=upstream.status_code,
                headers=response_headers,
                media_type=content_type,
            )
        except Exception as exc:
            logger.error("【光鸭 STRM】播放桥接失败 %s：%s", remote_path, exc)
            return JSONResponse({"error": "stream bridge failed"}, status_code=502)

    def status(self) -> Dict[str, Any]:
        plugin, error = self._source_plugin()
        return {
            **dict(self._last_status),
            "enabled": self._enabled,
            "source_ready": plugin is not None,
            "source_error": error,
            "base_url_configured": bool(self._base_url),
        }

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/play",
                "endpoint": self.play,
                "methods": ["GET", "HEAD"],
                "allow_anonymous": True,
                "summary": "光鸭 STRM 播放桥接",
            },
            {
                "path": "/sync",
                "endpoint": self.sync,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即同步 STRM",
            },
            {
                "path": "/status",
                "endpoint": self.status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看光鸭 STRM 状态",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        services = []
        instance_id = self.__class__.__name__
        if self._enabled:
            services.append({
                "id": f"{instance_id}.Sync",
                "name": "光鸭 STRM 定时同步",
                "trigger": IntervalTrigger(minutes=self._interval_minutes),
                "func": self.sync,
                "kwargs": {},
            })
        if self._onlyonce:
            services.append({
                "id": f"{instance_id}.RunOnce",
                "name": "光鸭 STRM 立即同步",
                "trigger": DateTrigger(run_date=datetime.now().astimezone() + timedelta(seconds=5)),
                "func": self._run_once,
                "kwargs": {},
            })
        return services

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用定时同步"}}],
                            },
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "保存后立即同步一次"}}],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info", "variant": "tonal",
                            "text": "依赖已经登录的光鸭云盘助手。不下载媒体主体，只生成 STRM；当前播放数据会经过 MoviePilot 转发。",
                        },
                    },
                    {"component": "VTextField", "props": {"model": "source_plugin_id", "label": "光鸭插件 ID", "placeholder": "ShukGuangYaDisk"}},
                    {"component": "VTextField", "props": {"model": "remote_root", "label": "光鸭云盘媒体根目录", "placeholder": "/emby"}},
                    {"component": "VTextField", "props": {"model": "output_root", "label": "STRM 输出目录（MoviePilot 容器内）", "placeholder": "/opt/emby/strm/光鸭云盘"}},
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "base_url", "label": "MoviePilot 地址（Emby 必须能访问）",
                            "placeholder": "http://moviepilot:3000",
                            "hint": "同 Docker 网络可填容器名；否则填写 Emby 能访问的内网地址。",
                            "persistentHint": True,
                        },
                    },
                    {"component": "VTextField", "props": {"model": "interval_minutes", "label": "同步间隔（分钟）", "type": "number", "min": 1}},
                    {"component": "VSwitch", "props": {"model": "cleanup_stale", "label": "删除远端已不存在的旧 STRM"}},
                    {"component": "VTextField", "props": {"model": "video_extensions", "label": "视频扩展名", "placeholder": ".mkv,.mp4,.ts,.m2ts"}},
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "stream_secret", "label": "播放签名密钥", "type": "password",
                            "hint": "首次自动生成；修改后需要重新同步 STRM。",
                            "persistentHint": True,
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "source_plugin_id": "ShukGuangYaDisk",
            "remote_root": "/emby",
            "output_root": "/opt/emby/strm/光鸭云盘",
            "base_url": "",
            "interval_minutes": 10,
            "cleanup_stale": True,
            "stream_secret": "",
            "video_extensions": ".mkv,.mp4,.avi,.mov,.wmv,.flv,.ts,.m2ts,.webm,.iso,.mpg,.mpeg",
        }

    def get_page(self) -> List[dict]:
        status = self.status()
        alert_type = "success" if status.get("success") else "info"
        if status.get("success") is False:
            alert_type = "error"
        text = (
            f"状态：{status.get('message') or '等待同步'}\n"
            f"最近同步：{status.get('last_sync') or '尚未执行'}\n"
            f"媒体：{status.get('files', 0)}，新建：{status.get('created', 0)}，"
            f"更新：{status.get('updated', 0)}，删除：{status.get('deleted', 0)}\n"
            f"光鸭插件：{'已就绪' if status.get('source_ready') else '未就绪'}"
        )
        return [
            {"component": "VAlert", "props": {"type": alert_type, "variant": "tonal", "text": text}},
            {
                "component": "VAlert",
                "props": {
                    "type": "warning", "variant": "tonal",
                    "text": "v1.0.0 播放数据由 MoviePilot 转发，不占本地影视存储，但会占用 MoviePilot 服务器带宽。",
                },
            },
        ]

    def stop_service(self):
        self._enabled = False
