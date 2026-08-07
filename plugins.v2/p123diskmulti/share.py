"""
P123DiskMulti 分享增量同步模块

功能：
1. 分享内容检查：遍历 123 分享（分页 + 递归 + 防环），统计文件/目录/大小
2. 增量转存：通过 123 服务器端直传（share_fs_copy，云端到云端，不占本地带宽），
   只转存分享中尚未同步过的新文件；转存结果通过轮询目标路径确认

设计原则：
- 只依赖 P123MultiApi（存储层）与 p123client，不依赖插件主类
- 身份键 = sha256(分享指纹 + FileId + Etag + Size)，与路径无关，文件改名不重复转存
- 已转存记录持久化到 SQLite（WAL），增量查询 O(1)
- 分享密码不写入日志；文件名/路径做注入校验；目标路径强制盘前缀
- 后台同步（start_sync）与 strm.py 同一模式：防重入锁 + 状态快照 + 完成回调
"""

import hashlib
import sqlite3
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

from p123client import check_response

from app.log import logger

# 分享列表分页大小（123 上限 100）
SHARE_PAGE_SIZE = 100
# 单批转存文件数（保守，123 限制 100）
DEFAULT_BATCH_SIZE = 50
# 转存确认轮询次数与间隔（秒）
DEFAULT_CONFIRM_ATTEMPTS = 6
DEFAULT_CONFIRM_INTERVAL = 2.0


@dataclass
class ShareFile:
    """分享文件项（已做安全校验的规范化数据）"""

    file_id: str
    name: str
    path: str  # 分享内 POSIX 路径（/ 开头）
    is_dir: bool
    size: int
    etag: str
    s3_key_flag: str
    parent_file_id: str
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


class ShareDB:
    """
    已转存文件记录（SQLite，WAL 模式）

    线程安全：单连接 + 互斥锁；写事务批量提交。
    """

    def __init__(self, db_path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS share_files (
                file_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                name TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                etag TEXT NOT NULL DEFAULT '',
                share_fp TEXT NOT NULL,
                target_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'done',
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_share_task ON share_files(task_id)"
        )
        self._conn.commit()

    def has(self, file_key: str) -> bool:
        """判断身份键是否已记录。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM share_files WHERE file_key = ?", (file_key,)
            ).fetchone()
            return row is not None

    def add(
        self,
        file_key: str,
        task_id: str,
        name: str,
        rel_path: str,
        size: int,
        etag: str,
        share_fp: str,
        target_path: str,
        status: str = "done",
    ) -> None:
        """写入转存记录（幂等，存在则更新）。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO share_files
                    (file_key, task_id, name, rel_path, size, etag,
                     share_fp, target_path, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_key) DO UPDATE SET
                    status = excluded.status,
                    target_path = excluded.target_path
                """,
                (
                    file_key, task_id, name, rel_path, int(size or 0),
                    etag, share_fp, target_path, status,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            self._conn.commit()

    def pending_keys(self, task_id: str) -> List[str]:
        """返回指定任务下所有待确认记录的 file_key。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT file_key FROM share_files WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            ).fetchall()
            return [row[0] for row in rows]

    def pending_all(self, task_id: str) -> List[Dict[str, Any]]:
        """返回指定任务下所有待确认记录的完整数据。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT file_key, name, rel_path, size, etag, target_path "
                "FROM share_files WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            ).fetchall()
            return [
                {
                    "file_key": r[0], "name": r[1], "rel_path": r[2],
                    "size": r[3], "etag": r[4], "target_path": r[5],
                }
                for r in rows
            ]

    def drop(self, file_key: str) -> None:
        """删除记录（pending 确认失败后重新转存前调用）。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM share_files WHERE file_key = ?", (file_key,)
            )
            self._conn.commit()

    def count(self, task_id: str = "") -> int:
        """已转存文件总数（可限定任务）。"""
        with self._lock:
            if task_id:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM share_files WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM share_files"
                ).fetchone()
            return int(row[0] or 0)

    def clear_task(self, task_id: str) -> None:
        """清空指定任务的记录（重新全量转存）。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM share_files WHERE task_id = ?", (task_id,)
            )
            self._conn.commit()

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


def share_fingerprint(share_key: str) -> str:
    """
    分享指纹（用于身份命名空间，不包含提取码）

    URL 链接规范化为 scheme+host+path（去掉 pwd 类 query 参数）后哈希，
    纯分享码直接哈希。提取码变化不影响指纹，避免修改密码后重复转存。
    """
    value = str(share_key or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(value)
            parsed_q = parse_qs(parsed.query, keep_blank_values=True)
            clean = {
                k: v
                for k, v in parsed_q.items()
                if k.lower() not in ("pwd", "password", "share_pwd", "提取码")
            }
            value = (
                f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
                f"{parsed.path.rstrip('/')}"
                + (("?" + urlencode(clean, doseq=True)) if clean else "")
            )
        except Exception:
            pass
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_key(share_fp: str, file_id: str, etag: str, size: Any) -> str:
    """文件身份键：分享指纹 + FileId + Etag + Size（与路径无关）。"""
    raw = f"{share_fp}\0{file_id}\0{etag}\0{int(size or 0)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ShareSync:
    """
    单个 123 分享的增量同步任务

    :param api: P123MultiApi 实例（多盘合并存储层）
    :param task_id: 任务唯一 ID（用于持久化命名空间）
    :param name: 任务显示名称
    :param share_key: 分享链接 URL 或分享码
    :param share_pwd: 分享提取码（可空）
    :param target_vpath: 转存目标虚拟目录，必须带盘前缀（如 /盘A/分享）
    :param db_path: SQLite 数据库文件路径（所有任务共用同一文件）
    :param batch_size: 单批转存文件数上限
    :param confirm_attempts: 转存确认轮询次数
    :param confirm_interval: 转存确认轮询间隔（秒）
    """

    def __init__(
        self,
        api,
        task_id: str,
        name: str,
        share_key: str,
        share_pwd: str,
        target_vpath: str,
        db_path,
        batch_size: int = DEFAULT_BATCH_SIZE,
        confirm_attempts: int = DEFAULT_CONFIRM_ATTEMPTS,
        confirm_interval: float = DEFAULT_CONFIRM_INTERVAL,
        auto_switch: bool = False,
        reserve_size: int = 0,
    ) -> None:
        self._api = api
        self.task_id = str(task_id)
        self.name = str(name)
        # 分享码规范化：URL 提取最后一段 + query 中的提取码
        raw_key = str(share_key or "").strip()
        share_pwd = str(share_pwd or "")
        if raw_key.startswith(("http://", "https://")):
            try:
                parsed = urlsplit(raw_key)
                raw_key = parsed.path.rstrip("/").rsplit("/", 1)[-1]
                if not share_pwd:
                    share_pwd = (parse_qs(parsed.query).get("pwd") or [""])[0]
            except Exception:
                pass
        self.share_key = raw_key
        self.share_pwd = share_pwd
        self.target_vpath = str(target_vpath or "").strip().rstrip("/") or "/"
        self._share_fp = share_fingerprint(str(share_key or "").strip())
        # 空间不足自动切换目标网盘（与上传自动切换共用同一套空间判定）
        self._auto_switch = bool(auto_switch)
        self._reserve_size = int(reserve_size or 0)
        self._batch_size = max(1, min(100, int(batch_size)))
        self._confirm_attempts = max(1, int(confirm_attempts))
        self._confirm_interval = max(0.0, float(confirm_interval))
        self._db = ShareDB(db_path)
        # 后台同步状态
        self._sync_lock = threading.Lock()
        self._sync_running = False
        self._last_result: Optional[Dict[str, Any]] = None
        self._last_time: Optional[str] = None
        self._on_done_ref = None
        # 校验目标目录盘前缀
        if self._share_fp and self._target_account() is None:
            raise ValueError(
                f"分享 {self.name} 的目标目录必须指向已配置的网盘（如 /盘A/分享）"
            )

    # ==================== 分享遍历 ====================

    def _client(self):
        """目标盘账号客户端（转存到哪个盘就用哪个盘的凭据）。"""
        account = self._target_account()
        if not account:
            raise RuntimeError(f"分享 {self.name} 的目标网盘不可用")
        return account.client

    def _target_account(self):
        """解析目标虚拟路径对应的网盘账号。"""
        accounts = getattr(self._api, "_accounts", []) or []
        if not self.target_vpath or self.target_vpath == "/":
            return None
        first = self.target_vpath.strip("/").split("/", 1)[0]
        for acc in accounts:
            if acc.name == first:
                return acc
        return None

    def _ensure_target_disk(self, need: int) -> bool:
        """
        确保目标网盘有足够空间容纳 need 字节（含预留空间）

        空间不足且启用自动切换时，切换到剩余空间最大的可用网盘
        （同时更新 target_vpath 盘前缀，后续映射/账号/确认自动跟随）；
        未启用自动切换或所有网盘均不足时返回 False。
        """
        api = self._api
        account = self._target_account()
        if not account:
            return False
        # 强制刷新空间信息（服务器端直传会消耗目标盘空间）
        try:
            api._invalidate_usage(account)
            api._invalidate_usage()
        except Exception:
            pass
        if api._has_space(account, need):
            return True
        if not self._auto_switch:
            logger.warn(
                f"【123多盘】分享 {self.name} 目标网盘 {account.name} 剩余空间不足"
                f"（需 {need} 字节），未启用自动切换，本批跳过"
            )
            return False
        alt = api._pick_disk(need, exclude=account)
        if not alt:
            logger.error(
                f"【123多盘】分享 {self.name} 所有网盘剩余空间均不足"
                f"（需 {need} 字节 + 预留 {self._reserve_size} 字节）"
            )
            return False
        # 切换目标盘前缀（保留原目录结构）
        parts = self.target_vpath.strip("/").split("/", 1)
        self.target_vpath = f"/{alt.name}" + (
            f"/{parts[1]}" if len(parts) > 1 else ""
        )
        logger.info(
            f"【123多盘】分享 {self.name} 目标网盘 {account.name} 空间不足，"
            f"自动切换到 {alt.name}，后续文件转存至 {self.target_vpath}"
        )
        return True

    @staticmethod
    def _safe_name(raw: Any) -> str:
        """校验分享文件名，拒绝注入字符。"""
        name = str(raw or "").strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
            raise ValueError("分享文件名非法")
        return name

    def list_share(self) -> List[ShareFile]:
        """
        递归分页遍历分享全部内容

        :return: 分享内全部文件与目录（path 为分享内 POSIX 路径）
        :raises RuntimeError: 分享不可访问
        """
        share_key = self.share_key
        if not share_key:
            raise RuntimeError("缺少分享链接或分享码")
        client = self._client()
        result: List[ShareFile] = []
        visited: set = set()

        def walk(parent_id: Any, parent_path: str) -> None:
            """递归列出一个分享目录（分页 + 防环）。"""
            if str(parent_id) in visited:
                return
            visited.add(str(parent_id))
            page = 1
            while True:
                resp = client.share_fs_list(
                    {
                        "ShareKey": share_key,
                        "SharePwd": self.share_pwd,
                        "parentFileId": parent_id,
                        "limit": SHARE_PAGE_SIZE,
                        "Page": page,
                    }
                )
                check_response(resp)
                data = resp.get("data") or {}
                if not isinstance(data, dict):
                    break
                items = data.get("InfoList") or data.get("infoList") or data.get("list") or []
                if not items:
                    break
                for raw in items:
                    name = self._safe_name(
                        raw.get("FileName") or raw.get("name") or raw.get("file_name") or ""
                    )
                    is_dir = str(raw.get("Type", 0)) in ("1", "True") or bool(raw.get("is_dir"))
                    path = str(PurePosixPath(parent_path) / name)
                    item = ShareFile(
                        file_id=str(
                            raw.get("FileId") or raw.get("file_id") or raw.get("id") or ""
                        ),
                        name=name,
                        path=path,
                        is_dir=is_dir,
                        size=int(raw.get("Size") or raw.get("size") or 0),
                        etag=str(raw.get("Etag") or raw.get("etag") or raw.get("md5") or ""),
                        s3_key_flag=str(
                            raw.get("S3KeyFlag") or raw.get("s3_key_flag") or ""
                        ),
                        parent_file_id=str(
                            raw.get("ParentFileId") or raw.get("parent_file_id") or "0"
                        ),
                        raw=dict(raw),
                    )
                    if is_dir:
                        result.append(item)
                        walk(item.file_id, item.path)
                    else:
                        result.append(item)
                # 页未满或 Next==-1 表示最后一页
                if len(items) < SHARE_PAGE_SIZE or str(data.get("Next") or "") == "-1":
                    break
                page += 1

        walk(0, "/")
        return result

    # ==================== 内容检查 ====================

    def check(self) -> Dict[str, Any]:
        """
        检查分享内容（可访问性 + 统计）

        :return: {success, name, message, files, dirs, total_size, root_items}
        """
        try:
            items = self.list_share()
        except Exception as e:
            logger.error(f"【123多盘】分享 {self.name} 检查失败: {e}")
            return {
                "success": False,
                "name": self.name,
                "message": str(e),
                "files": 0, "dirs": 0, "total_size": 0, "root_items": [],
            }
        files = [it for it in items if not it.is_dir]
        dirs = [it for it in items if it.is_dir]
        root_items = [
            {"name": it.name, "is_dir": it.is_dir, "size": it.size}
            for it in items
            if it.path.count("/") == 1
        ]
        total = sum(it.size for it in files)
        logger.info(
            f"【123多盘】分享 {self.name} 检查完成：{len(files)} 个文件，"
            f"{len(dirs)} 个目录，总大小 {total} B"
        )
        return {
            "success": True,
            "name": self.name,
            "message": f"可访问：{len(files)} 个文件 / {len(dirs)} 个目录",
            "files": len(files),
            "dirs": len(dirs),
            "total_size": total,
            "root_items": root_items,
        }

    # ==================== 增量转存 ====================

    def start_run(
        self, on_done: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> bool:
        """
        立刻检测转存：后台执行一轮完整的「内容检测 + 增量转存」

        与 start_sync 共用防重入锁（结果包含 check 检测统计，一次遍历分享）。

        :return: True 已启动；False 已有任务在运行
        """
        return self.start_sync(on_done=on_done)

    def start_sync(
        self, on_done: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> bool:
        """
        后台增量转存：立即返回，转存在守护线程中执行

        :param on_done: 完成回调（参数为结果 dict），后台线程中调用
        :return: True 已启动；False 已有任务在运行
        """
        if not self._sync_lock.acquire(blocking=False):
            return False
        if on_done is not None:
            self._on_done_ref = (
                weakref.WeakMethod(on_done)
                if hasattr(on_done, "__self__")
                else weakref.ref(on_done)
            )
        else:
            self._on_done_ref = None
        threading.Thread(
            target=self._sync_worker,
            daemon=True,
            name=f"P123ShareSync-{self.task_id}",
        ).start()
        return True

    def _sync_worker(self) -> None:
        """后台转存工作线程（持有 _sync_lock）。"""
        self._sync_running = True
        self._last_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = self._sync_unlocked()
            self._last_result = result
            cb = self._on_done_ref() if self._on_done_ref else None
            if cb:
                try:
                    cb(result)
                except Exception as e:
                    logger.warn(f"【123多盘】分享 {self.name} 完成回调失败: {e}")
        except Exception as e:
            self._last_result = {
                "success": False,
                "scanned": 0, "copied": 0, "skipped": 0,
                "failed": 1, "pending": 0, "errors": [str(e)],
            }
            logger.error(f"【123多盘】分享 {self.name} 后台转存异常: {e}")
        finally:
            self._sync_running = False
            self._sync_lock.release()

    def sync(self) -> Dict[str, Any]:
        """
        同步执行增量转存（已有任务运行时直接返回忙碌）

        :return: {success, scanned, copied, skipped, failed, pending, errors}
        """
        if not self._sync_lock.acquire(blocking=False):
            return {
                "success": False, "scanned": 0, "copied": 0, "skipped": 0,
                "failed": 0, "pending": 0,
                "errors": ["已有转存任务正在进行，请稍后再试"],
            }
        try:
            return self._sync_unlocked()
        finally:
            self._sync_lock.release()

    def _sync_unlocked(self) -> Dict[str, Any]:
        """
        增量转存核心实现（调用方须持有 _sync_lock）

        流程：确认历史 pending → 遍历分享 → 过滤已转存 → 按目标目录分批
        服务器端直传 → 轮询确认；未确认的记 pending 下轮处理。
        """
        result: Dict[str, Any] = {
            "success": True,
            "scanned": 0, "copied": 0, "skipped": 0,
            "failed": 0, "pending": 0, "errors": [],
        }
        try:
            # 1. 确认历史 pending（存在则转正，不存在则删除以便重新转存）
            result["copied"] += self._confirm_pending()
            # 2. 遍历分享（顺带生成内容检测统计：一次遍历同时完成检测与转存）
            items = self.list_share()
            files = [it for it in items if not it.is_dir]
            dirs = [it for it in items if it.is_dir]
            result["check"] = {
                "success": True,
                "name": self.name,
                "message": f"可访问：{len(files)} 个文件 / {len(dirs)} 个目录",
                "files": len(files),
                "dirs": len(dirs),
                "total_size": sum(it.size or 0 for it in files),
                "root_items": [
                    {"name": it.name, "is_dir": it.is_dir, "size": it.size}
                    for it in items
                    if it.path.count("/") == 1
                ],
            }
            result["scanned"] = len(files)
            # 3. 过滤已转存 + 映射目标路径
            plans: List[Tuple[ShareFile, str]] = []
            for f in files:
                key = file_key(self._share_fp, f.file_id, f.etag, f.size)
                if self._db.has(key):
                    result["skipped"] += 1
                    continue
                target = self._map_target(f.path)
                plans.append((f, target))
            if not plans:
                logger.info(
                    f"【123多盘】分享 {self.name} 无新文件（共 {result['scanned']} 个，"
                    f"已同步 {result['skipped']} 个）"
                )
                return result
            # 4. 按目标父目录分批转存（每批前做空间检查，不足自动切换网盘）
            by_parent: Dict[str, List[ShareFile]] = {}
            for f, _target in plans:
                by_parent.setdefault(
                    str(PurePosixPath(_target).parent), []
                ).append(f)
            for _parent, group in by_parent.items():
                for offset in range(0, len(group), self._batch_size):
                    files = group[offset:offset + self._batch_size]
                    try:
                        # 空间预检 + 自动切换（切盘后 target_vpath 前缀已更新）
                        need = sum(f.size or 0 for f in files)
                        if not self._ensure_target_disk(need):
                            result["failed"] += len(files)
                            result["errors"].append(
                                f"目标网盘剩余空间不足，跳过 {len(files)} 个文件"
                            )
                            continue
                        # 按切盘后的目标路径重新映射并取父目录
                        targets = [self._map_target(f.path) for f in files]
                        parent = str(PurePosixPath(targets[0]).parent)
                        parent_item = self._api.get_folder(PurePosixPath(parent))
                        if not parent_item:
                            result["failed"] += len(files)
                            result["errors"].append(f"目标目录创建失败: {parent}")
                            continue
                        chunk = list(zip(files, targets))
                        copied = self._copy_batch(parent_item, chunk)
                        confirmed, pending = self._confirm_batch(
                            chunk, parent_item, copied
                        )
                        result["copied"] += confirmed
                        result["pending"] += pending
                    except Exception as e:
                        result["failed"] += len(files)
                        result["errors"].append(f"转存批次失败: {e}")
            logger.info(
                f"【123多盘】分享 {self.name} 转存完成：扫描 {result['scanned']}，"
                f"新转存 {result['copied']}，跳过 {result['skipped']}，"
                f"失败 {result['failed']}，待确认 {result['pending']}"
            )
            return result
        except Exception as e:
            logger.error(f"【123多盘】分享 {self.name} 转存失败: {e}")
            result["success"] = False
            result["failed"] = max(1, result["failed"])
            result["check"] = {
                "success": False,
                "name": self.name,
                "message": str(e),
                "files": 0, "dirs": 0, "total_size": 0, "root_items": [],
            }
            result["errors"].append(str(e))
            return result

    def _map_target(self, share_path: str) -> str:
        """分享内路径 → 目标虚拟路径（保留完整目录结构）。"""
        rel = str(share_path).lstrip("/")
        if not rel:
            return self.target_vpath
        return f"{self.target_vpath}/{rel}"

    def _copy_batch(
        self,
        parent_item,
        chunk: List[Tuple[ShareFile, str]],
    ) -> Optional[List[Tuple[str, ShareFile, str]]]:
        """
        提交一批服务器端直传转存

        :return: [(file_key, ShareFile, target), ...]；请求失败抛异常
        """
        file_list = []
        mapped: List[Tuple[str, ShareFile, str]] = []
        for f, target in chunk:
            key = file_key(self._share_fp, f.file_id, f.etag, f.size)
            file_list.append(
                {
                    "file_id": f.file_id,
                    "file_name": PurePosixPath(target).name,
                    "etag": f.etag,
                    "parent_file_id": f.parent_file_id,
                    "size": f.size,
                }
            )
            mapped.append((key, f, target))
        resp = self._client().share_fs_copy(
            {
                "share_key": self.share_key,
                "share_pwd": self.share_pwd,
                "file_list": file_list,
            },
            parent_id=parent_item.fileid,
        )
        check_response(resp)
        return mapped

    def _confirm_batch(
        self,
        chunk: List[Tuple[ShareFile, str]],
        parent_item,
        mapped: List[Tuple[str, ShareFile, str]],
    ) -> Tuple[int, int]:
        """
        轮询确认一批转存结果

        123 的转存为异步执行，提交后轮询目标路径直到文件可见。
        :return: (确认成功数, 待确认数)
        """
        unresolved: Dict[str, Tuple[ShareFile, str]] = {
            key: (f, target) for key, f, target in mapped
        }
        confirmed_keys: List[str] = []
        for _attempt in range(self._confirm_attempts):
            for key, (f, target) in list(unresolved.items()):
                item = self._api.get_item(PurePosixPath(target))
                if item and item.name == PurePosixPath(target).name:
                    confirmed_keys.append(key)
                    unresolved.pop(key, None)
            if not unresolved:
                break
            if self._confirm_interval:
                time.sleep(self._confirm_interval)
        # 落库：确认的记 done，未确认的记 pending（下轮先确认再决定重转）
        for key, f, target in mapped:
            status = "done" if key in confirmed_keys else "pending"
            self._db.add(
                file_key=key,
                task_id=self.task_id,
                name=f.name,
                rel_path=f.path,
                size=f.size,
                etag=f.etag,
                share_fp=self._share_fp,
                target_path=target,
                status=status,
            )
        if confirmed_keys:
            # 转存占用目标盘空间，刷新空间缓存
            try:
                self._api._invalidate_usage()
            except Exception:
                pass
        return len(confirmed_keys), len(unresolved)

    def _confirm_pending(self) -> int:
        """
        确认历史 pending 记录

        目标已存在 → 转正（+1）；目标不存在 → 删除记录，本轮重新转存。
        :return: 转正数量
        """
        resolved = 0
        for row in self._db.pending_all(self.task_id):
            target = row["target_path"]
            item = None
            if target:
                item = self._api.get_item(PurePosixPath(target))
            if item and item.name == PurePosixPath(target).name:
                self._db.add(
                    file_key=row["file_key"],
                    task_id=self.task_id,
                    name=row["name"],
                    rel_path=row["rel_path"],
                    size=row["size"],
                    etag=row["etag"],
                    share_fp=self._share_fp,
                    target_path=target,
                    status="done",
                )
                resolved += 1
            else:
                self._db.drop(row["file_key"])
        return resolved

    # ==================== 状态 ====================

    def status(self) -> Dict[str, Any]:
        """任务状态快照。"""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "enabled": True,
            "share_key": self._mask_share_key(),
            "target_vpath": self.target_vpath,
            "running": self._sync_running,
            "last_time": self._last_time,
            "last_result": self._last_result,
            "synced": self._db.count(self.task_id),
        }

    def _mask_share_key(self) -> str:
        """脱敏分享标识（URL 去除 query 后取尾部 6 位）。"""
        value = self.share_key
        if value.startswith(("http://", "https://")):
            try:
                value = urlsplit(value).path.rstrip("/").rsplit("/", 1)[-1]
            except Exception:
                pass
        return f"****{value[-6:]}" if len(value) > 6 else "****"

    def close(self) -> None:
        """释放资源。"""
        self._db.close()
