"""
P123DiskMulti 定期目录整理模块（调用 MoviePilot 自带整理链）

职责：
1. 递归扫描指定 123 盘目录（带盘前缀），收集媒体文件
2. 逐个调用 MoviePilot 整理链 TransferChain.manual_transfer 提交到整理队列
   —— 重命名 / 刮削 / 进媒体库等全部遵循 MoviePilot 自身整理规则
   （目标目录由 MoviePilot 的转移目录配置决定；文件操作走本插件的
     StorageOperSelection 存储实现，同盘移动为 123 服务端秒级 fs_move）
3. 支持 cron 定时 + 手动触发，后台执行、结果可查、非重入

设计原则（可维护 / 可扩展）：
- 只依赖 P123MultiApi（存储层）与 MoviePilot 整理链，不依赖插件主类
- 后台执行模式与 strm.py 保持一致（锁 + 守护线程 + 弱引用回调 + 状态快照）
- 提交使用 background=True，实际整理由 MoviePilot 队列异步完成并自带历史去重，
  重复触发不会重复移动已整理文件
"""

import threading
import weakref
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from app.log import logger
from app.schemas import FileItem

# 视为媒体的扩展名（不含点，与 MoviePilot 整理链的媒体判定基本对齐）
MEDIA_EXTS = [
    "mp4", "mkv", "ts", "iso", "rmvb", "avi", "mov",
    "mpeg", "mpg", "wmv", "3gp", "asf", "m4v", "flv",
    "m2ts", "tp", "f4v",
]
# 蓝光原盘结构目录名（整理时跳过，避免按原盘结构误整理）
BLURAY_DIR_NAMES = {"BDMV", "CERTIFICATE"}
# 默认定时表达式
DEFAULT_ORGANIZE_CRON = "0 3 * * *"


class OrganizeRunner:
    """
    定期调用 MoviePilot 整理链整理指定 123 盘目录

    :param api: P123MultiApi 实例（多盘合并存储层）
    :param media_exts: 视为媒体的扩展名列表（不含点）
    """

    def __init__(
        self,
        api,
        media_exts: Optional[List[str]] = None,
    ):
        self._api = api
        self._media_exts = set(media_exts or MEDIA_EXTS)
        # 后台执行状态
        self._lock = threading.Lock()
        self._running = False
        self._last_time: Optional[str] = None
        self._last_result: Optional[Dict] = None
        self._on_done_ref = None
        # 因目标已有更好文件而自动删除的源文件累计数
        self._deleted_count = 0

    # ---------------------------------------------------------------- 外部入口

    def start_organize(
        self,
        paths_text: str,
        on_done: Optional[Callable[[Dict], None]] = None,
    ) -> bool:
        """
        后台异步整理：立即返回，整理提交在守护线程中默默执行

        :param paths_text: 目录配置（每行一个 123 盘目录，需带盘前缀）
        :param on_done: 完成后回调（参数为结果 dict），在后台线程中调用
        :return: True 已启动后台整理；False 已有整理任务在运行
        """
        if not self._lock.acquire(blocking=False):
            return False
        if on_done is not None:
            # 绑定方法用 WeakMethod，普通函数/静态方法用 weakref.ref
            if hasattr(on_done, "__self__"):
                self._on_done_ref = weakref.WeakMethod(on_done)
            else:
                self._on_done_ref = weakref.ref(on_done)
        else:
            self._on_done_ref = None
        threading.Thread(
            target=self._organize_worker,
            args=(paths_text,),
            daemon=True,
            name="P123MultiOrganize",
        ).start()
        return True

    def organize_status(self) -> Dict:
        """
        后台整理状态快照

        :return: {"running": bool, "last_time": str|None,
                  "last_result": dict|None}
        """
        return {
            "running": self._running,
            "last_time": self._last_time,
            "last_result": self._last_result,
            "deleted": self._deleted_count,
        }

    # ---------------------------------------------------------------- 后台线程

    def _organize_worker(self, paths_text: str):
        """
        后台整理工作线程（持有 _lock）
        """
        self._running = True
        self._last_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = self._organize_unlocked(paths_text)
            self._last_result = result
            # 完成后回调（弱引用回调可能已被回收）
            cb = self._on_done_ref() if self._on_done_ref else None
            if cb:
                try:
                    cb(result)
                except Exception as e:
                    logger.warn(f"【123多盘整理】整理完成回调执行失败: {e}")
        except Exception as e:
            self._last_result = {
                "ok": 0,
                "fail": 1,
                "errors": [f"后台整理异常: {e}"],
                "paths": [],
            }
            logger.error(f"【123多盘整理】后台整理异常: {e}")
        finally:
            self._running = False
            self._lock.release()

    # ---------------------------------------------------------------- 核心实现

    def _organize_unlocked(self, paths_text: str) -> Dict:
        """
        整理核心实现（调用方须持有 _lock）

        对每个配置目录：
        1. 确认目录存在（get_item，不会自动建目录）
        2. 递归收集媒体文件（跳过蓝光原盘结构目录）
        3. 逐个提交到 MoviePilot 整理队列（background=True）

        :return: {"ok": 提交数, "fail": 失败数, "errors": 错误列表, "paths": 处理的目录}
        """
        result: Dict = {"ok": 0, "fail": 0, "errors": [], "paths": []}
        paths = [
            line.strip()
            for line in (paths_text or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for raw_path in paths:
            path_str = raw_path.strip().rstrip("/") or "/"
            dir_item = self._api.get_item(path_str)
            if dir_item is None or dir_item.type != "dir":
                result["fail"] += 1
                result["errors"].append(f"目录不存在或不是文件夹: {path_str}")
                logger.warning(f"【123多盘整理】跳过无效目录: {path_str}")
                continue
            result["paths"].append(path_str)
            logger.info(f"【123多盘整理】开始整理目录: {path_str}")
            for fileitem in self._walk_media_files(dir_item):
                ok, errmsg = self._submit(fileitem)
                if ok:
                    result["ok"] += 1
                    logger.info(f"【123多盘整理】已提交整理: {fileitem.path}")
                else:
                    result["fail"] += 1
                    result["errors"].append(f"{fileitem.path}: {errmsg}")
                    logger.warning(
                        f"【123多盘整理】提交失败: {fileitem.path} - {errmsg}"
                    )
        logger.info(
            f"【123多盘整理】完成: 提交 {result['ok']} 个，失败 {result['fail']} 个"
        )
        return result

    def _walk_media_files(self, dir_item: FileItem) -> Iterable[FileItem]:
        """
        递归遍历目录，产出所有媒体文件（跳过蓝光原盘结构目录）

        :param dir_item: 起始目录项
        """
        try:
            children = self._api.list(dir_item) or []
        except Exception as e:
            logger.warning(f"【123多盘整理】遍历目录失败 {dir_item.path}: {e}")
            return
        for child in children:
            if child.type == "dir":
                if child.name in BLURAY_DIR_NAMES:
                    continue
                yield from self._walk_media_files(child)
            else:
                ext = (
                    child.name.rsplit(".", 1)[-1].lower()
                    if "." in child.name
                    else ""
                )
                if ext in self._media_exts:
                    yield child

    def _submit(self, fileitem: FileItem) -> Tuple[bool, str]:
        """
        提交单个文件到 MoviePilot 整理队列

        :param fileitem: 123 盘媒体文件项（storage=123云盘）
        :return: (是否提交成功, 错误信息)
        """
        try:
            # 延迟导入，避免模块加载时依赖整理链（也便于测试替换）
            from app.chain.transfer import TransferChain

            state, errmsg = TransferChain().manual_transfer(
                fileitem=fileitem, background=True
            )
            return bool(state), str(errmsg)
        except Exception as e:
            return False, f"整理链调用异常: {e}"

    # ---------------------------------------------------------------- 失败自动清理

    def handle_transfer_failed(self, event_data: Dict) -> bool:
        """
        处理 MoviePilot 整理失败事件：源文件在 123 盘且失败原因为目标已有更好文件时，
        删除网盘上的低版本源文件（移入回收站，可恢复）

        :param event_data: TransferFailed 事件数据（fileitem / transferinfo 等）
        :return: True 已删除；False 未处理
        """
        try:
            if not isinstance(event_data, dict):
                return False
            transferinfo = event_data.get("transferinfo")
            if transferinfo is None:
                return False
            # 只有失败事件才处理
            if getattr(transferinfo, "success", True):
                return False
            # 失败原因必须是目标已有更好的文件
            message = str(getattr(transferinfo, "message", "") or "")
            if "质量更好" not in message:
                return False
            fileitem = event_data.get("fileitem")
            if not fileitem or getattr(fileitem, "storage", "") != getattr(
                self._api, "disk_name", "123云盘"
            ):
                return False
            path = getattr(fileitem, "path", "")
            if not path:
                return False
            # 确认源文件仍存在（可能已被其他流程处理）
            item = self._api.get_item(path)
            if item is None:
                logger.info(
                    f"【123多盘整理】源文件已不存在，跳过清理: {path}"
                )
                return False
            # 删除（移入回收站）
            if not self._api.delete(item):
                logger.error(f"【123多盘整理】删除低版本文件失败: {path}")
                return False
            self._deleted_count += 1
            logger.info(
                f"【123多盘整理】目标已有更好文件，已删除网盘低版本文件（回收站可恢复）: {path}"
            )
            return True
        except Exception as e:
            logger.error(f"【123多盘整理】整理失败自动清理异常: {e}")
            return False
