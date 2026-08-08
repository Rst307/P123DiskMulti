"""
自分享目录（v1.4.6）：把自己网盘里的媒体目录建成 123 分享，STRM 播放走分享票

背景：分享票播放（share/download/info）天然支持多 IP 下载，是 123 分享的设计内
行为，不触发账号级风控。此前分享票只能用于「转存他人分享」的文件（share_files
表记录）；自分享目录把「自己上传的文件」也纳入分享票通道：

    配置自分享目录 -> 自动建分享（share/create）-> 遍历索引分享内文件
    （shareKey/FileId/S3KeyFlag）-> STRM 播放按 md5 反查并换票

通道说明：建分享 / 查分享列表 / 遍历分享 / 换票统一走 web 前端通道
api.123278.com/b。yun.123pan.com 为第三方挂载专用通道（风控严格，2026-08
全库 403 50002 事件即由该通道触发），不用于本功能。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from p123client import check_response

from .strm import DEFAULT_MEDIA_EXTS

# 全部分享相关 API 统一走 web 前端通道（票身份/风控最宽松）
SELF_SHARE_API_BASE = "https://api.123278.com/b"
SHARE_PAGE_SIZE = 100
# 永久有效（与 123 网页端「永久有效」一致）
EXPIRE_FOREVER = "9999-12-31T23:59:59+08:00"


class SelfShareDB:
    """
    自分享目录索引（SQLite WAL，单连接+互斥锁）

    与 ShareDB 共用同一个数据库文件（p123sharesync.sqlite3），
    两张表：
    - self_share_dirs：每个自分享目录的分享元数据（shareKey/shareId/链接）
    - self_share_files：分享内媒体文件索引（STRM 播放按 etag+size 反查）
    """

    def __init__(self, db_path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS self_share_dirs (
                dir_path TEXT PRIMARY KEY,
                account TEXT NOT NULL DEFAULT '',
                dir_file_id TEXT NOT NULL DEFAULT '',
                share_id TEXT NOT NULL DEFAULT '',
                share_key TEXT NOT NULL DEFAULT '',
                share_pwd TEXT NOT NULL DEFAULT '',
                share_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                file_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS self_share_files (
                dir_path TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                etag TEXT NOT NULL DEFAULT '',
                share_key TEXT NOT NULL DEFAULT '',
                share_pwd TEXT NOT NULL DEFAULT '',
                file_id TEXT NOT NULL DEFAULT '',
                s3_key_flag TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (dir_path, rel_path)
            )
            """
        )
        # STRM 播放热路径反查索引
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_self_share_etag "
            "ON self_share_files(etag, size)"
        )
        self._conn.commit()

    def get_dir(self, dir_path: str) -> Optional[Dict[str, Any]]:
        """查询目录分享元数据（无则 None）"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM self_share_dirs WHERE dir_path = ?",
                (dir_path,),
            ).fetchone()
        if not row:
            return None
        cols = [d[1] for d in self._conn.execute(
            "PRAGMA table_info(self_share_dirs)"
        )]
        return dict(zip(cols, row))

    def set_dir(self, dir_path: str, **fields: Any) -> None:
        """写入/更新目录分享元数据"""
        merged = {
            "dir_path": dir_path,
            "account": fields.get("account", ""),
            "dir_file_id": fields.get("dir_file_id", ""),
            "share_id": fields.get("share_id", ""),
            "share_key": fields.get("share_key", ""),
            "share_pwd": fields.get("share_pwd", ""),
            "share_url": fields.get("share_url", ""),
            "status": fields.get("status", ""),
            "message": fields.get("message", ""),
            "file_count": int(fields.get("file_count", 0)),
            "updated_at": fields.get(
                "updated_at", datetime.now().isoformat(timespec="seconds")
            ),
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO self_share_dirs (
                    dir_path, account, dir_file_id, share_id, share_key,
                    share_pwd, share_url, status, message, file_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(merged[k] for k in (
                    "dir_path", "account", "dir_file_id", "share_id",
                    "share_key", "share_pwd", "share_url", "status",
                    "message", "file_count", "updated_at",
                )),
            )
            self._conn.commit()

    def delete_dir(self, dir_path: str) -> None:
        """删除目录分享元数据及其文件索引"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM self_share_dirs WHERE dir_path = ?", (dir_path,)
            )
            self._conn.execute(
                "DELETE FROM self_share_files WHERE dir_path = ?", (dir_path,)
            )
            self._conn.commit()

    def upsert_files(self, dir_path: str, files: List[Dict[str, Any]],
                     share_key: str, share_pwd: str) -> None:
        """批量写入分享内文件索引（同目录同路径覆盖）"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO self_share_files (
                    dir_path, rel_path, name, size, etag, share_key,
                    share_pwd, file_id, s3_key_flag, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dir_path,
                        f["rel_path"],
                        f.get("name", ""),
                        int(f.get("size", 0)),
                        f.get("etag", ""),
                        share_key,
                        share_pwd,
                        f.get("file_id", ""),
                        f.get("s3_key_flag", ""),
                        now,
                    )
                    for f in files
                ],
            )
            self._conn.commit()

    def prune_files(self, dir_path: str, keep_rel_paths) -> None:
        """删除分享内已不存在的文件索引（keep 为当前有效的 rel_path 集合）"""
        keep = set(keep_rel_paths or [])
        with self._lock:
            if not keep:
                self._conn.execute(
                    "DELETE FROM self_share_files WHERE dir_path = ?",
                    (dir_path,),
                )
            else:
                placeholders = ",".join("?" for _ in keep)
                self._conn.execute(
                    f"DELETE FROM self_share_files WHERE dir_path = ? "
                    f"AND rel_path NOT IN ({placeholders})",
                    (dir_path, *sorted(keep)),
                )
            self._conn.commit()

    def find_by_etag(self, etag: str, size: int) -> List[Dict[str, Any]]:
        """按 etag(md5)+size 反查分享内文件索引（最新优先）"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM self_share_files
                WHERE etag = ? AND size = ?
                ORDER BY updated_at DESC
                """,
                (etag or "", int(size or 0)),
            ).fetchall()
        cols = [d[1] for d in self._conn.execute(
            "PRAGMA table_info(self_share_files)"
        )]
        return [dict(zip(cols, row)) for row in rows]

    def dir_rows(self) -> List[Dict[str, Any]]:
        """全部目录分享元数据（按路径排序）"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM self_share_dirs ORDER BY dir_path"
            ).fetchall()
        cols = [d[1] for d in self._conn.execute(
            "PRAGMA table_info(self_share_dirs)"
        )]
        return [dict(zip(cols, row)) for row in rows]


class SelfShareManager:
    """
    自分享目录管理器

    - 配置：set_dirs([(目录虚拟路径, 提取码), ...])
    - 同步：sync_dir / sync_all（建分享 -> 遍历索引）
    - 播放：find_by_etag 供 STRM 分享票模式按 md5 反查
    - 运维：rebuild（取消旧分享并重新建分享）、status
    """

    def __init__(
        self,
        api,
        db_path,
        media_exts: Optional[List[str]] = None,
    ) -> None:
        self._api = api
        self._media_exts = set(
            ext.strip().lower().lstrip(".")
            for ext in (media_exts or DEFAULT_MEDIA_EXTS)
            if ext and ext.strip()
        )
        self._db = SelfShareDB(db_path)
        self._dirs: List[Tuple[str, str]] = []  # [(目录虚拟路径, 提取码)]
        self._busy = False

    # ==================== 配置 ====================

    def set_dirs(self, items: List[Tuple[str, str]]) -> None:
        """设置自分享目录列表：[(目录虚拟路径, 提取码), ...]"""
        self._dirs = [(str(d or "").strip(), str(p or "").strip()) for d, p in items]

    def dirs(self) -> List[Tuple[str, str]]:
        return list(self._dirs)

    def has_dir(self, dir_path: str) -> bool:
        """目录是否已建立分享（含元数据行）"""
        return self._db.get_dir(dir_path) is not None

    # ==================== 播放反查 ====================

    def find_by_etag(self, etag: str, size: int) -> List[Dict[str, Any]]:
        """
        按 md5(etag)+size 反查自分享记录（STRM 播放热路径）

        :return: 记录 dict，键与 share.py 转存记录对齐：
            dir_path/rel_path/name/size/etag/share_key/share_pwd/
            file_id/share_s3_key_flag/created_at
        """
        result = []
        for r in self._db.find_by_etag(etag or "", int(size or 0)):
            result.append(
                {
                    "dir_path": r["dir_path"],
                    "rel_path": r["rel_path"],
                    "name": r["name"],
                    "size": r["size"],
                    "etag": r["etag"],
                    "share_key": r["share_key"],
                    "share_pwd": r["share_pwd"],
                    "file_id": r["file_id"],
                    "share_s3_key_flag": r["s3_key_flag"],
                    "created_at": r["updated_at"],
                }
            )
        return result

    # ==================== 同步 ====================

    def sync_all(self) -> Dict[str, Any]:
        """同步全部已配置目录"""
        summary: Dict[str, Any] = {"ok": 0, "fail": 0, "results": []}
        if self._busy:
            logger.warn("【123多盘】自分享同步正在进行中，跳过本次")
            return summary
        self._busy = True
        try:
            for vpath, pwd in self._dirs:
                try:
                    summary["results"].append(self.sync_dir(vpath, pwd))
                    summary["ok"] += 1
                except Exception as e:
                    summary["fail"] += 1
                    summary["results"].append(
                        {"dir": vpath, "ok": False, "error": str(e)}
                    )
                    logger.error(f"【123多盘】自分享目录 {vpath} 同步失败: {e}")
        finally:
            self._busy = False
        return summary

    def sync_dir(self, vpath: str, pwd: str = "") -> Dict[str, Any]:
        """
        同步单个自分享目录

        1. 解析网盘目录（虚拟路径 -> 账号 + 目录 FileId）
        2. 复用已有分享（先尝试遍历，失败再校验分享列表决定是否重建）
        3. 索引分享内媒体文件（upsert + 清理已移除文件）

        :raises Exception: 目录不存在 / 分享不可用 / 网络异常
        """
        vpath = str(vpath or "").strip()
        if not vpath:
            raise ValueError("目录为空")
        account, real = self._api._split(vpath)
        if not account:
            raise ValueError(f"目录 {vpath} 未匹配到任何网盘账号")
        client = account.client
        dir_name = real.rstrip("/").rsplit("/", 1)[-1] or account.name
        try:
            dir_id = self._api._path_to_id(account, real)
        except FileNotFoundError:
            raise FileNotFoundError(f"网盘目录不存在: {vpath}")
        except Exception as e:
            raise RuntimeError(f"解析网盘目录失败: {vpath} - {e}")

        row = self._db.get_dir(vpath)
        share = None
        files: List[Dict[str, Any]] = []
        if row and row.get("share_key"):
            # 已有分享：先尝试遍历（遍历成功即视为分享有效）
            try:
                files = self._walk(
                    client, row["share_key"], row.get("share_pwd") or pwd or ""
                )
                share = {
                    "share_id": row.get("share_id") or "",
                    "share_key": row["share_key"],
                    "share_pwd": row.get("share_pwd") or pwd or "",
                }
            except Exception as e:
                logger.warn(
                    f"【123多盘】自分享 {vpath} 遍历失败（{e}），校验分享是否失效"
                )
                still = False
                if row.get("share_id"):
                    try:
                        still = self._find_share(client, row["share_id"]) is not None
                    except Exception:
                        still = False
                if still:
                    raise RuntimeError(
                        f"自分享 {vpath} 遍历失败但分享仍存在（可能为网络异常）: {e}"
                    )
                logger.warn(f"【123多盘】自分享 {vpath} 分享已失效，自动重建")
        if not share:
            share = self._create_share(
                client, dir_id, dir_name, pwd,
                old_share_id=row.get("share_id") if row else None,
            )
            files = self._walk(client, share["share_key"], share["share_pwd"])

        share_key = share["share_key"]
        share_pwd = share["share_pwd"] or pwd or ""
        media = [
            {
                "rel_path": f["rel_path"],
                "name": f["name"],
                "size": f["size"],
                "etag": f["etag"],
                "file_id": f["file_id"],
                "s3_key_flag": f["s3_key_flag"],
            }
            for f in files
            if self._is_media(f["name"]) and f["etag"]
        ]
        self._db.upsert_files(vpath, media, share_key=share_key, share_pwd=share_pwd)
        self._db.prune_files(vpath, {m["rel_path"] for m in media})
        self._db.set_dir(
            vpath,
            account=account.name,
            dir_file_id=str(dir_id),
            share_id=str(share.get("share_id") or ""),
            share_key=share_key,
            share_pwd=share_pwd,
            share_url=self._share_url(share_key, share_pwd),
            status="ok",
            message="",
            file_count=len(media),
        )
        logger.info(
            f"【123多盘】自分享目录 {vpath} 就绪："
            f"{self._share_url(share_key, share_pwd)}（索引 {len(media)} 个媒体文件）"
        )
        return {
            "dir": vpath,
            "ok": True,
            "share_url": self._share_url(share_key, share_pwd),
            "files": len(media),
            "total_files": len(files),
        }

    def rebuild(self, vpath: Optional[str] = None) -> List[str]:
        """
        重建自分享：取消旧分享并删除索引（vpath 为空则全部目录）

        :return: 需要重新同步的目录列表（调用方随后 sync）
        """
        targets = [
            d for d, _ in self._dirs if not vpath or d == vpath
        ]
        done = []
        for d in targets:
            try:
                row = self._db.get_dir(d)
                if row and row.get("share_id"):
                    try:
                        account, _real = self._api._split(d)
                        if account:
                            account.client.share_cancel(
                                row["share_id"], base_url=SELF_SHARE_API_BASE
                            )
                            logger.info(f"【123多盘】已取消旧分享 {row['share_id']}")
                    except Exception as e:
                        logger.warn(f"【123多盘】取消旧分享失败（忽略）: {e}")
                self._db.delete_dir(d)
                done.append(d)
            except Exception as e:
                logger.error(f"【123多盘】自分享 {d} 重建准备失败: {e}")
        return done

    def status(self) -> List[Dict[str, Any]]:
        """全部自分享目录状态（含分享链接与索引数）"""
        return self._db.dir_rows()

    # ==================== 内部实现 ====================

    def _is_media(self, name: str) -> bool:
        """是否媒体文件（按扩展名）"""
        ext = str(name or "").rsplit(".", 1)[-1].lower() if "." in str(name) else ""
        return ext in self._media_exts

    def _walk(self, client, share_key: str, share_pwd: str) -> List[Dict[str, Any]]:
        """
        递归分页遍历分享内容

        :return: [{rel_path, name, size, etag, file_id, s3_key_flag}, ...]
        :raises Exception: 分享不可访问（失效/网络异常）
        """
        result: List[Dict[str, Any]] = []
        visited = set()

        def walk(parent_id: Any, parent_path: str) -> None:
            if str(parent_id) in visited:
                return
            visited.add(str(parent_id))
            page = 1
            while True:
                resp = client.share_fs_list(
                    {
                        "ShareKey": share_key,
                        "SharePwd": share_pwd,
                        "parentFileId": parent_id,
                        "limit": SHARE_PAGE_SIZE,
                        "Page": page,
                    },
                    base_url=SELF_SHARE_API_BASE,
                )
                check_response(resp)
                data = resp.get("data") or {}
                if not isinstance(data, dict):
                    break
                items = (
                    data.get("InfoList")
                    or data.get("infoList")
                    or data.get("list")
                    or []
                )
                if not items:
                    break
                for raw in items:
                    name = str(
                        raw.get("FileName")
                        or raw.get("name")
                        or raw.get("file_name")
                        or ""
                    )
                    if not name or name in (".", ".."):
                        continue
                    is_dir = str(raw.get("Type", 0)) in ("1", "True") or bool(
                        raw.get("is_dir")
                    )
                    rel = str(PurePosixPath(parent_path) / name)
                    if is_dir:
                        walk(str(raw.get("FileId") or raw.get("file_id") or ""), rel)
                    else:
                        result.append(
                            {
                                "rel_path": rel,
                                "name": name,
                                "size": int(raw.get("Size") or raw.get("size") or 0),
                                "etag": str(
                                    raw.get("Etag")
                                    or raw.get("etag")
                                    or raw.get("md5")
                                    or ""
                                ),
                                "file_id": str(
                                    raw.get("FileId") or raw.get("file_id") or ""
                                ),
                                "s3_key_flag": str(
                                    raw.get("S3KeyFlag")
                                    or raw.get("s3_key_flag")
                                    or ""
                                ),
                            }
                        )
                if len(items) < SHARE_PAGE_SIZE or str(data.get("Next") or "") == "-1":
                    break
                page += 1

        walk(0, "/")
        return result

    def _find_share(self, client, share_id: str) -> Optional[Dict[str, str]]:
        """
        在账号分享列表中按 shareId 查找分享

        :return: {share_id, share_key, share_pwd}；未找到返回 None
        """
        want = str(share_id)
        page = 1
        while True:
            resp = client.share_list(
                {"Page": page, "limit": SHARE_PAGE_SIZE},
                base_url=SELF_SHARE_API_BASE,
            )
            check_response(resp)
            data = resp.get("data") or {}
            items = data.get("InfoList") or data.get("infoList") or []
            for raw in items:
                rid = str(raw.get("ShareId") or raw.get("shareId") or "")
                if rid == want:
                    return {
                        "share_id": rid,
                        "share_key": str(
                            raw.get("ShareKey") or raw.get("shareKey") or ""
                        ),
                        "share_pwd": str(
                            raw.get("SharePwd") or raw.get("sharePwd") or ""
                        ),
                    }
            if len(items) < SHARE_PAGE_SIZE or str(data.get("Next") or "") == "-1":
                break
            page += 1
        return None

    def _find_share_by_name(self, client, share_name: str) -> Optional[Dict[str, str]]:
        """按分享名称查找（新建后兜底用，取最新一条）"""
        page = 1
        while True:
            resp = client.share_list(
                {"Page": page, "limit": SHARE_PAGE_SIZE},
                base_url=SELF_SHARE_API_BASE,
            )
            check_response(resp)
            data = resp.get("data") or {}
            items = data.get("InfoList") or data.get("infoList") or []
            for raw in items:
                if str(raw.get("ShareName") or raw.get("shareName") or "") == share_name:
                    return {
                        "share_id": str(
                            raw.get("ShareId") or raw.get("shareId") or ""
                        ),
                        "share_key": str(
                            raw.get("ShareKey") or raw.get("shareKey") or ""
                        ),
                        "share_pwd": str(
                            raw.get("SharePwd") or raw.get("sharePwd") or ""
                        ),
                    }
            if len(items) < SHARE_PAGE_SIZE or str(data.get("Next") or "") == "-1":
                break
            page += 1
        return None

    def _create_share(
        self,
        client,
        dir_id: Any,
        dir_name: str,
        pwd: str,
        old_share_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        创建分享（web 通道 api.123278.com/b）

        :param old_share_id: 旧分享 id（重建时先取消，避免残留）
        :return: {share_id, share_key, share_pwd}
        :raises Exception: 创建失败
        """
        if old_share_id:
            try:
                client.share_cancel(old_share_id, base_url=SELF_SHARE_API_BASE)
                logger.info(f"【123多盘】已取消旧分享 {old_share_id}")
            except Exception as e:
                logger.warn(f"【123多盘】取消旧分享失败（忽略）: {e}")
        resp = client.share_create(
            {
                "fileIdList": str(dir_id),
                "shareName": dir_name,
                "sharePwd": pwd or "",
                # 未设置提取码时禁止系统自动填充，避免出现未知提取码
                "fillPwdSwitch": 1 if pwd else 0,
                "expiration": EXPIRE_FOREVER,
                # 关闭免登录流量限制（媒体库长时间播放，避免分享中途失效）
                "trafficLimitSwitch": 1,
                # 游客免登录提取关闭（本插件始终携带 token 换票，不受影响）
                "trafficSwitch": 1,
            },
            base_url=SELF_SHARE_API_BASE,
        )
        check_response(resp)
        data = resp.get("data") or {}
        share_id = str(
            data.get("ShareId") or data.get("shareId") or data.get("share_id") or ""
        )
        share_key = str(
            data.get("ShareKey") or data.get("shareKey") or data.get("share_key") or ""
        )
        share_pwd = str(
            data.get("SharePwd") or data.get("sharePwd") or data.get("share_pwd") or ""
        ) or pwd or ""
        if not share_key:
            # 兜底：创建成功但未返回 shareKey 时，从分享列表按名称匹配
            found = self._find_share_by_name(client, dir_name)
            if not found or not found.get("share_key"):
                raise RuntimeError("创建分享成功但未取到 shareKey（分享列表中也未找到）")
            share_id = found.get("share_id") or share_id
            share_key = found["share_key"]
            share_pwd = found.get("share_pwd") or share_pwd
        logger.info(
            f"【123多盘】已创建自分享: {self._share_url(share_key, share_pwd)}"
        )
        return {
            "share_id": share_id,
            "share_key": share_key,
            "share_pwd": share_pwd,
        }

    @staticmethod
    def _share_url(share_key: str, share_pwd: str = "") -> str:
        """分享链接（展示用）"""
        url = f"https://www.123pan.com/s/{share_key}"
        return f"{url}?pwd={share_pwd}" if share_pwd else url
