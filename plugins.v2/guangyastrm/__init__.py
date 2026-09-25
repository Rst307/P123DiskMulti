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

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from app.core.event import Event, eventmanager
from app.core.plugin import PluginManager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class GuangYaStrm(_PluginBase):
    plugin_name = "光鸭 STRM"
    plugin_desc = "无需挂载光鸭云盘，直接扫描远程目录生成 STRM，并复用光鸭插件流式播放。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.3.0"
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

    # 自动整理：扫描光鸭远端“待整理目录”，提交给 MoviePilot 原生整理链。
    # 最终目标目录由 MoviePilot 的存储/整理目录规则决定，例如：
    # 光鸭云盘助手:/emby_raw -> 光鸭云盘助手:/emby
    _organize_enabled = False
    _organize_onlyonce = False
    _organize_paths = ""
    _organize_cron = "*/10 * * * *"
    _organize_skip_bluray = True
    _organize_auto_strm = True

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

        self._organize_enabled = bool(config.get("organize_enabled", False))
        self._organize_onlyonce = bool(config.get("organize_onlyonce", False))
        self._organize_paths = str(config.get("organize_paths") or "")
        self._organize_cron = str(config.get("organize_cron") or "*/10 * * * *").strip()
        self._organize_skip_bluray = bool(config.get("organize_skip_bluray", True))
        self._organize_auto_strm = bool(config.get("organize_auto_strm", True))

        self._sync_lock = threading.Lock()
        if not hasattr(self, "_organize_lock"):
            self._organize_lock = threading.Lock()
        if not hasattr(self, "_organize_running"):
            self._organize_running = False
        if not hasattr(self, "_organize_last_time"):
            self._organize_last_time = None
        if not hasattr(self, "_organize_last_result"):
            self._organize_last_result = None
        if not hasattr(self, "_last_auto_strm"):
            self._last_auto_strm = None

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
            "organize_enabled": self._organize_enabled,
            "organize_onlyonce": self._organize_onlyonce if onlyonce is None else False,
            "organize_paths": self._organize_paths,
            "organize_cron": self._organize_cron,
            "organize_skip_bluray": self._organize_skip_bluray,
            "organize_auto_strm": self._organize_auto_strm,
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

    def start_sync(self) -> Dict[str, Any]:
        """立即在后台执行一次全量 STRM 同步。"""
        if self._sync_lock.locked():
            return {"success": False, "message": "已有 STRM 同步任务正在运行"}
        threading.Thread(
            target=self.sync,
            daemon=True,
            name="GuangYaStrmSyncNow",
        ).start()
        return {
            "success": True,
            "background": True,
            "message": "已开始后台扫描媒体目录并生成 STRM",
        }

    def _run_once(self):
        result = self.sync()
        self._onlyonce = False
        self._save_config(onlyonce=False)
        return result

    def _organize_path_list(self) -> List[str]:
        """解析待整理光鸭目录，每行一个远端路径。"""
        result = []
        seen = set()
        for raw in (self._organize_paths or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                path = self._normalize_remote(line)
            except ValueError:
                logger.warning("【光鸭 STRM】【自动整理】忽略非法目录: %s", line)
                continue
            if path not in seen:
                result.append(path)
                seen.add(path)
        return result

    def _walk_organize_media(self, api: Any, dir_item: Any) -> Iterable[Any]:
        """递归遍历光鸭目录，只产出 MoviePilot 可整理的视频 FileItem。"""
        try:
            children = api.list(dir_item) or []
        except Exception as exc:
            logger.warning(
                "【光鸭 STRM】【自动整理】遍历目录失败 %s: %s",
                getattr(dir_item, "path", ""),
                exc,
            )
            return

        extensions = self._extensions()
        for child in children:
            if getattr(child, "type", None) == "dir":
                if (
                    self._organize_skip_bluray
                    and str(getattr(child, "name", "")).upper() in {"BDMV", "CERTIFICATE"}
                ):
                    continue
                yield from self._walk_organize_media(api, child)
                continue

            name = str(getattr(child, "name", "") or "")
            if PurePosixPath(name).suffix.casefold() in extensions:
                yield child

    @staticmethod
    def _submit_to_moviepilot(fileitem: Any) -> Tuple[bool, str]:
        """
        使用 MoviePilot 原生整理链提交文件。
        识别、重命名、目标目录、冲突处理、刮削等均遵循 MoviePilot 配置；
        光鸭插件的 StorageOperSelection 负责实际云端 move/copy。
        """
        try:
            from app.chain.transfer import TransferChain

            state, message = TransferChain().manual_transfer(
                fileitem=fileitem,
                background=True,
            )
            return bool(state), str(message or "")
        except Exception as exc:
            return False, f"整理链调用异常: {exc}"

    def _organize_worker(self):
        self._organize_running = True
        self._organize_last_time = datetime.now().astimezone().isoformat(timespec="seconds")
        result = {
            "success": True,
            "submitted": 0,
            "failed": 0,
            "scanned": 0,
            "paths": [],
            "errors": [],
        }
        try:
            plugin, error = self._source_plugin()
            if not plugin:
                raise RuntimeError(error or "光鸭云盘助手不可用")
            api = getattr(plugin, "_guangya_api", None)
            if not api:
                raise RuntimeError("光鸭云盘助手存储 API 不可用")

            paths = self._organize_path_list()
            if not paths:
                raise RuntimeError("未配置待整理光鸭目录")

            seen = set()
            for path in paths:
                dir_item = api.get_item(Path(path))
                if not dir_item or getattr(dir_item, "type", None) != "dir":
                    result["failed"] += 1
                    result["errors"].append(f"目录不存在或不是文件夹: {path}")
                    logger.warning("【光鸭 STRM】【自动整理】跳过无效目录: %s", path)
                    continue

                result["paths"].append(path)
                logger.info("【光鸭 STRM】【自动整理】开始扫描: %s", path)
                for fileitem in self._walk_organize_media(api, dir_item):
                    file_path = str(getattr(fileitem, "path", "") or "")
                    identity = (
                        getattr(fileitem, "fileid", None)
                        or getattr(fileitem, "id", None)
                        or file_path
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    result["scanned"] += 1

                    # 防止因第三方 FileItem 构造异常导致 MoviePilot 无法选中光鸭存储。
                    if not getattr(fileitem, "storage", None):
                        try:
                            fileitem.storage = getattr(plugin, "_disk_name", "光鸭云盘助手")
                        except Exception:
                            pass

                    ok, message = self._submit_to_moviepilot(fileitem)
                    if ok:
                        result["submitted"] += 1
                        logger.info("【光鸭 STRM】【自动整理】已提交: %s", file_path)
                    else:
                        result["failed"] += 1
                        detail = f"{file_path}: {message}"
                        result["errors"].append(detail)
                        logger.warning("【光鸭 STRM】【自动整理】提交失败: %s", detail)

            result["success"] = result["failed"] == 0 or result["submitted"] > 0
            result["message"] = (
                f"扫描 {result['scanned']} 个媒体，提交 {result['submitted']} 个，"
                f"失败 {result['failed']} 个"
            )
            logger.info("【光鸭 STRM】【自动整理】%s", result["message"])
        except Exception as exc:
            result["success"] = False
            result["message"] = str(exc)
            result["errors"].append(str(exc))
            logger.error("【光鸭 STRM】【自动整理】执行失败: %s", exc)
        finally:
            result["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._organize_last_result = result
            self._organize_running = False
            self._organize_lock.release()

    def start_organize(self) -> Dict[str, Any]:
        """立即后台整理一次；手动执行不要求开启 Cron 自动整理。"""
        if not self._organize_path_list():
            return {"success": False, "message": "未配置待整理光鸭目录"}
        if not self._organize_lock.acquire(blocking=False):
            return {"success": False, "message": "已有自动整理任务正在运行"}
        threading.Thread(
            target=self._organize_worker,
            daemon=True,
            name="GuangYaStrmOrganize",
        ).start()
        return {
            "success": True,
            "background": True,
            "message": "已开始后台扫描光鸭目录并提交到 MoviePilot 整理队列",
        }

    def organize_status(self) -> Dict[str, Any]:
        return {
            "success": True,
            "enabled": self._organize_enabled,
            "running": self._organize_running,
            "paths": self._organize_path_list(),
            "cron": self._organize_cron,
            "last_time": self._organize_last_time,
            "last_result": self._organize_last_result,
        }

    def _run_organize_once(self):
        try:
            return self.start_organize()
        finally:
            self._organize_onlyonce = False
            self._save_config(onlyonce=False)

    def _is_organize_source(self, fileitem: Any, disk_name: str) -> bool:
        """判断整理完成事件是否来自本插件配置的待整理目录。"""
        if not fileitem:
            return False
        storage = str(getattr(fileitem, "storage", "") or "")
        if storage and storage != disk_name:
            return False
        try:
            source_path = PurePosixPath(
                self._normalize_remote(getattr(fileitem, "path", "") or "")
            )
        except ValueError:
            return False
        for root_text in self._organize_path_list():
            try:
                source_path.relative_to(PurePosixPath(root_text))
                return True
            except ValueError:
                continue
        return False

    def _generate_target_strm(self, target_item: Any) -> Optional[str]:
        """为一个已经整理完成的目标文件立即生成 STRM。"""
        remote_path = self._normalize_remote(getattr(target_item, "path", "") or "")
        relative = self._relative(remote_path)
        if relative is None:
            logger.info("【光鸭 STRM】【整理联动】目标不在媒体根目录内，跳过: %s", remote_path)
            return None
        if PurePosixPath(remote_path).suffix.casefold() not in self._extensions():
            return None

        relative_strm = relative.with_suffix(".strm")
        target = self._safe_output(relative_strm)
        state = self._write_text(target, self._stream_url(remote_path))

        # 把联动生成的文件加入索引；若此时正有全量同步，索引由全量同步统一落盘。
        if self._sync_lock.acquire(blocking=False):
            try:
                index = self._load_index()
                index[remote_path] = relative_strm.as_posix()
                self._write_index(index)
            finally:
                self._sync_lock.release()

        self._last_auto_strm = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "remote_path": remote_path,
            "strm_path": str(target),
            "state": state,
        }
        logger.info("【光鸭 STRM】【整理联动】STRM %s: %s", state, target)
        return str(target)

    @eventmanager.register(EventType.TransferComplete)
    def organize_transfer_complete(self, event: Event):
        """MoviePilot 真正完成整理后，按需立即生成该目标文件的 STRM。"""
        if not self._organize_auto_strm:
            return
        data = event.event_data or {}
        if not isinstance(data, dict):
            return

        plugin, _ = self._source_plugin()
        if not plugin:
            return
        disk_name = str(getattr(plugin, "_disk_name", "") or "光鸭云盘助手")

        source_item = data.get("fileitem")
        if not self._is_organize_source(source_item, disk_name):
            return

        transferinfo = data.get("transferinfo")
        target_item = getattr(transferinfo, "target_item", None)
        if not target_item:
            return
        if str(getattr(target_item, "storage", "") or "") != disk_name:
            return

        try:
            self._generate_target_strm(target_item)
        except Exception as exc:
            logger.error("【光鸭 STRM】【整理联动】自动生成 STRM 失败: %s", exc)

    def play(self, request: Request, path: str = "", sig: str = "") -> Response:
        """
        播放入口只负责鉴权、换取一次性光鸭签名地址并 302 跳转。
        视频主体不会经过 MoviePilot 进程。
        """
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

        try:
            api = getattr(plugin, "_guangya_api", None)
            client = getattr(plugin, "_client", None)
            if not api or not client:
                return JSONResponse({"error": "source plugin client unavailable"}, status_code=503)

            file_item = api.get_item(Path(remote_path))
            if not file_item or getattr(file_item, "type", None) != "file":
                return JSONResponse({"error": "file not found"}, status_code=404)

            # Emby 常先做 HEAD 探测。这里直接用光鸭元数据回答，
            # 避免为了 HEAD 再让 MoviePilot 去读取任何媒体内容。
            if request.method.upper() == "HEAD":
                headers = {
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-store",
                    "X-GuangYa-Playback": "direct-302",
                }
                size = int(getattr(file_item, "size", 0) or 0)
                if size > 0:
                    headers["Content-Length"] = str(size)
                return Response(status_code=200, headers=headers)

            # GET 时只向光鸭 API 请求一个短期 signedURL，然后立即 302。
            # Emby 随后直接访问 signedURL，MoviePilot 不再代理视频数据。
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

            logger.info("【光鸭 STRM】302 直链播放: %s", remote_path)
            return RedirectResponse(
                url=str(download_url),
                status_code=302,
                headers={
                    "Cache-Control": "no-store",
                    "X-GuangYa-Playback": "direct-302",
                },
            )
        except Exception as exc:
            logger.error("【光鸭 STRM】302 换链失败 %s：%s", remote_path, exc)
            return JSONResponse({"error": "redirect failed"}, status_code=502)

    def status(self) -> Dict[str, Any]:
        plugin, error = self._source_plugin()
        return {
            **dict(self._last_status),
            "enabled": self._enabled,
            "source_ready": plugin is not None,
            "source_error": error,
            "base_url_configured": bool(self._base_url),
            "organize_auto_strm": self._organize_auto_strm,
            "last_auto_strm": self._last_auto_strm,
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
            {
                "path": "/organize/run",
                "endpoint": self.start_organize,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行一次光鸭远端目录自动整理",
            },
            {
                "path": "/organize/status",
                "endpoint": self.organize_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看自动整理状态",
            },
            {
                "path": "/action/sync",
                "endpoint": self.start_sync,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即后台生成 STRM",
            },
            {
                "path": "/action/organize",
                "endpoint": self.start_organize,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即后台整理",
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

        if self._organize_enabled and self._organize_path_list() and self._organize_cron:
            try:
                organize_trigger = CronTrigger.from_crontab(self._organize_cron)
                services.append({
                    "id": f"{instance_id}.Organize",
                    "name": "光鸭云盘自动整理",
                    "trigger": organize_trigger,
                    "func": self.start_organize,
                    "kwargs": {},
                })
            except Exception as exc:
                logger.error(
                    "【光鸭 STRM】【自动整理】Cron 表达式无效 %s: %s",
                    self._organize_cron,
                    exc,
                )

        if self._organize_onlyonce:
            services.append({
                "id": f"{instance_id}.OrganizeRunOnce",
                "name": "光鸭云盘立即整理",
                "trigger": DateTrigger(run_date=datetime.now().astimezone() + timedelta(seconds=8)),
                "func": self._run_organize_once,
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
                            "text": "依赖已经登录的光鸭云盘助手。不下载媒体主体，只生成 STRM；播放时 MoviePilot 仅换取签名地址并返回 302，视频主体直接从光鸭/CDN 读取。",
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
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "自动整理会递归扫描下面配置的光鸭远端目录，并把媒体文件提交给 MoviePilot 原生整理链。最终目标目录不在这里指定，而由 MoviePilot 的整理/存储目录配置决定。典型配置：/emby_raw → 光鸭云盘助手:/emby。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [{"component": "VSwitch", "props": {"model": "organize_enabled", "label": "启用光鸭云盘自动整理"}}],
                            },
                            {
                                "component": "VCol", "props": {"cols": 12, "md": 6},
                                "content": [{"component": "VSwitch", "props": {"model": "organize_onlyonce", "label": "保存后立即整理一次"}}],
                            },
                        ],
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "organize_paths",
                            "label": "待整理光鸭目录（每行一个）",
                            "placeholder": "/emby_raw",
                            "rows": 3,
                            "hint": "这里填写源目录。整理后的目标由 MoviePilot 的整理规则决定。",
                            "persistentHint": True,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "organize_cron",
                            "label": "自动整理 Cron",
                            "placeholder": "*/10 * * * *",
                            "hint": "默认每 10 分钟扫描一次；成功整理后文件会被 MoviePilot 移出源目录。",
                            "persistentHint": True,
                        },
                    },
                    {"component": "VSwitch", "props": {"model": "organize_skip_bluray", "label": "跳过 BDMV/CERTIFICATE 原盘结构目录"}},
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "organize_auto_strm",
                            "label": "整理完成后自动生成 STRM",
                            "hint": "MoviePilot 真正完成整理后，立即为目标文件生成 STRM，不必等下一次全量同步。",
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
            "organize_enabled": False,
            "organize_onlyonce": False,
            "organize_paths": "",
            "organize_cron": "*/10 * * * *",
            "organize_skip_bluray": True,
            "organize_auto_strm": True,
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
        organize = self.organize_status()
        organize_last = organize.get("last_result") or {}
        organize_text = (
            f"自动整理：{'运行中' if organize.get('running') else ('已启用' if organize.get('enabled') else '未启用')}\n"
            f"待整理目录：{', '.join(organize.get('paths') or []) or '未配置'}\n"
            f"最近执行：{organize.get('last_time') or '尚未执行'}\n"
            f"整理完成自动 STRM：{'开启' if self._organize_auto_strm else '关闭'}"
        )
        if organize_last:
            organize_text += (
                f"\n扫描：{organize_last.get('scanned', 0)}，"
                f"提交：{organize_last.get('submitted', 0)}，"
                f"失败：{organize_last.get('failed', 0)}"
            )

        actions = {
            "component": "div",
            "props": {"class": "d-flex flex-wrap ga-2 mb-3"},
            "content": [
                {
                    "component": "VBtn",
                    "props": {
                        "color": "primary",
                        "variant": "tonal",
                        "prepend-icon": "mdi-folder-cog",
                    },
                    "text": "立即整理",
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/action/organize",
                            "method": "get",
                        }
                    },
                },
                {
                    "component": "VBtn",
                    "props": {
                        "color": "success",
                        "variant": "tonal",
                        "prepend-icon": "mdi-file-link",
                    },
                    "text": "立即生成 STRM",
                    "events": {
                        "click": {
                            "api": f"plugin/{self.__class__.__name__}/action/sync",
                            "method": "get",
                        }
                    },
                },
            ],
        }

        return [
            actions,
            {"component": "VAlert", "props": {"type": alert_type, "variant": "tonal", "text": text}},
            {
                "component": "VAlert",
                "props": {
                    "type": "success", "variant": "tonal",
                    "text": "v1.3.0：MoviePilot 播放端仍为光鸭 302；外网如经 Cloudflare Tunnel，使用仓库 tools/guangya-emby302 伴侣网关可让视频主体直接走光鸭/CDN → 客户端。",
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "info", "variant": "tonal",
                    "text": organize_text,
                },
            },
        ]

    def stop_service(self):
        self._enabled = False
