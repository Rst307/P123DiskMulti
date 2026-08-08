"""
P123DiskMulti STRM 助手模块（多盘）

职责：
1. 302 播放端点支撑：S3KeyFlag -> DownloadUrl 实时换取
   （S3KeyFlag 是 123 全局文件标识，任意账号登录后均可换取，天然支持多盘）
2. 全量同步：递归扫描【所有网盘】媒体目录，批量生成 STRM 文件到本地
3. 监控整理：MoviePilot 转移完成后为入库文件生成 STRM

设计原则（可维护 / 可扩展）：
- 只依赖 P123MultiApi（存储层）与 MoviePilot 基础设施，不依赖插件主类
- 文件信息统一取自 FileItem.pickcode（123 原始数据：FileName/Size/Etag/S3KeyFlag）
- 路径映射格式与 p123strmhelper 一致：本地STRM目录#网盘媒体库目录（每行一条）
- 新增能力（分享 STRM / 媒体信息下载 / 字幕伴随文件）只需在此模块添加方法
"""

import ast
import threading
import weakref
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from p123client import check_response

from app.log import logger
from app.schemas import FileItem

from .share_ticket import (
    OnDemandShareCache,
    ShareTicketCache,
    get_on_demand_share_url,
    get_share_download_url,
)

# 默认媒体扩展名（与 p123strmhelper 保持一致）
DEFAULT_MEDIA_EXTS = [
    "mp4", "mkv", "ts", "iso", "rmvb", "avi", "mov",
    "mpeg", "mpg", "wmv", "3gp", "asf", "m4v", "flv",
    "m2ts", "tp", "f4v",
]
# 蓝光原盘目录名（全量同步时跳过，不生成 STRM）
BLURAY_DIR_NAMES = {"BDMV", "CERTIFICATE"}
# 秒传转存兜底目录名（无 S3KeyFlag 时使用，兼容旧版 STRM URL）
FAST_TRANSFER_DIR = "我的秒传"
# 默认 User-Agent（302 换取下载地址时使用）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class StrmHelper:
    """
    123云盘多盘 STRM 助手

    :param api: P123MultiApi 实例（多盘合并存储层）
    :param moviepilot_address: MoviePilot 对外访问地址（STRM URL 前缀）
    :param media_exts: 视为媒体的扩展名列表（不含点）
    """

    def __init__(
        self,
        api,
        moviepilot_address: str = "",
        media_exts: Optional[List[str]] = None,
        download_base_urls: Optional[List[str]] = None,
        download_probe: bool = True,
        download_cache_ttl: int = 600,
        ticket_mode: str = "vip",
        shares: Optional[list] = None,
        self_share=None,
        on_demand_share_days: int = 7,
        on_demand_share_pwd: str = "",
    ):
        self._api = api
        self._moviepilot_address = str(moviepilot_address or "").rstrip("/")
        self._media_exts = set(
            ext.strip().lower().lstrip(".")
            for ext in (media_exts or DEFAULT_MEDIA_EXTS)
            if ext.strip()
        )
        # 下载换链域名候选（None 用内置默认：api.123278.com/b 优先，yun 通道兜底）
        self._download_base_urls = (
            [u.strip() for u in (download_base_urls or []) if u and u.strip()]
            or None
        )
        # 是否探测下载票有效性并在通道被风控时自动切换域名
        self._download_probe = bool(download_probe)
        # 已验证下载直链缓存秒数（<=0 关闭）：命中不再签票，降低风控触发频率
        try:
            self._download_cache_ttl = int(download_cache_ttl or 0)
        except (TypeError, ValueError):
            self._download_cache_ttl = 600
        # 播放票模式：vip=VIP直链（默认，兼容旧行为）| share=分享票 |
        # auto=分享票优先+VIP兜底 | on_demand=按需分享（播放时懒建带有效期分享）
        self._ticket_mode = str(ticket_mode or "vip").strip().lower()
        if self._ticket_mode not in ("vip", "share", "auto", "on_demand"):
            self._ticket_mode = "vip"
        # 按需分享：有效期天数与提取码（懒建分享自动过期，到期重建）
        try:
            self._on_demand_share_days = int(on_demand_share_days or 7)
        except (TypeError, ValueError):
            self._on_demand_share_days = 7
        self._on_demand_share_pwd = str(on_demand_share_pwd or "")
        # 分享任务列表（分享票模式：md5 -> 分享记录 -> shareKey/FileId 反查用）
        self._shares = list(shares or [])
        # 自分享目录管理器（自己分享自己的文件，分享票播放）
        self._self_share = self_share
        # 分享票缓存（只缓存换票结果，每次播放仍做一次 210 解析）
        self._share_ticket_cache = ShareTicketCache()
        # 按需分享元数据缓存（分享到期自动重建）
        self._on_demand_share_cache = OnDemandShareCache()
        # 全量同步防重入锁
        self._sync_lock = threading.Lock()
        # 后台同步状态（start_full_sync 更新，sync_status 读取）
        self._sync_running = False
        self._sync_last_result: Optional[Dict] = None
        self._sync_last_time: Optional[str] = None
        # 后台同步完成回调（弱引用，避免与插件类循环引用）
        self._on_done_ref = None

    # ==================== URL 构建与换取 ====================

    def build_strm_url(
        self,
        name: str,
        size: int,
        md5: str,
        s3_key_flag: str,
        disk_name: str = "",
    ) -> Optional[str]:
        """
        构建 STRM 播放 URL（302 端点地址 + 文件参数）

        :param name: 文件名
        :param size: 文件大小（字节）
        :param md5: 文件 MD5（Etag）
        :param s3_key_flag: 123 文件标识
        :param disk_name: 所在网盘名称（用于 302 时优先使用该盘账号换取，可选）
        :return: STRM URL，未配置 MoviePilot 地址时返回 None
        """
        if not self._moviepilot_address:
            logger.error(
                "【123多盘STRM】未配置 MoviePilot 访问地址，无法生成 STRM URL"
            )
            return None
        from app.core.config import settings

        url = (
            f"{self._moviepilot_address}/api/v1/plugin/P123DiskMulti/redirect_url"
            f"?apikey={quote(settings.API_TOKEN)}"
            f"&name={quote(name)}&size={int(size)}&md5={quote(md5)}"
            f"&s3_key_flag={quote(s3_key_flag)}"
        )
        if disk_name:
            url += f"&disk={quote(disk_name)}"
        return url

    def resolve_download_url(
        self,
        name: str,
        size: int,
        md5: str,
        s3_key_flag: str,
        user_agent: str = "",
        disk_name: str = "",
    ) -> Optional[str]:
        """
        换取 123 实时下载地址（302 端点核心）

        优先使用文件所在网盘的账号换取；带 S3KeyFlag 时直接换取，
        无 S3KeyFlag（兼容旧版 STRM URL）时先秒传转存获取标识再换取。

        播放通道按 ticket_mode 选择：
        - vip：VIP 直链换链（默认，支持域名风控自动切换）
        - share：分享票（share/download/info），多 IP 播放为分享设计内行为，
          不受 VIP 通道风控影响；仅分享转存入库的文件可用
        - auto：分享票优先，失败自动回退 VIP 直链

        :return: 下载地址，失败返回 None
        """
        account = self._pick_account(disk_name)
        if not account:
            logger.error("【123多盘STRM】没有可用的网盘账号，无法换取下载地址")
            return None
        client = account.client
        try:
            # 分享票模式：先走分享通道（多 IP 下载是分享设计内行为，无账号级风控）
            if self._ticket_mode in ("share", "auto"):
                share_url = self._resolve_share_ticket_url(
                    name=name,
                    size=int(size or 0),
                    md5=md5,
                    user_agent=user_agent or DEFAULT_UA,
                    disk_name=disk_name,
                )
                if share_url:
                    return share_url
                if self._ticket_mode == "share":
                    logger.error(
                        "【123多盘STRM】分享票模式换取失败（原因见上方日志），"
                        "请检查分享状态或切换回 VIP 直链模式"
                    )
                    return None
                logger.warn(
                    "【123多盘STRM】分享票换取失败，自动回退 VIP 直链模式"
                )
            # 按需分享模式：播放时懒建带有效期分享（不依赖任何预建分享/索引）
            if self._ticket_mode == "on_demand":
                url = get_on_demand_share_url(
                    account,
                    disk_name=disk_name,
                    name=name,
                    md5=md5,
                    size=int(size or 0),
                    ttl_days=self._on_demand_share_days,
                    share_pwd=self._on_demand_share_pwd,
                    user_agent=user_agent or DEFAULT_UA,
                    ticket_cache=self._share_ticket_cache,
                    share_cache=self._on_demand_share_cache,
                )
                if not url:
                    logger.error(
                        "【123多盘STRM】按需分享模式换取失败（原因见上方日志），"
                        "请稍后重试或切换回 VIP 直链模式"
                    )
                    return None
                return url
            # VIP 直链换链（以下为原逻辑）
            # 无文件标识时：秒传转存兜底（虚拟转存，不消耗空间）
            if not s3_key_flag:
                if not md5 or not size:
                    logger.error("【123多盘STRM】缺少文件标识且缺少 md5/size，无法换取下载地址")
                    return None
                resp = client.fs_mkdir(FAST_TRANSFER_DIR)
                check_response(resp)
                resp = client.upload_file_fast(
                    file_md5=md5,
                    file_name=f"{md5}-{size}",
                    file_size=int(size),
                    parent_id=resp["data"]["Info"]["FileId"],
                    duplicate=2,
                )
                check_response(resp)
                s3_key_flag = resp["data"]["Info"]["S3KeyFlag"]
                logger.debug(
                    f"【123多盘STRM】秒传转存成功，获取 S3KeyFlag: {s3_key_flag}"
                )
            # 换取下载地址：自动验证票有效性，通道被风控（403 50002/1010）时自动切换换链域名
            url = client.get_download_url(
                {
                    "S3KeyFlag": s3_key_flag,
                    "FileName": name,
                    "Etag": md5,
                    "Size": int(size or 0),
                },
                headers={"User-Agent": user_agent or DEFAULT_UA},
                base_urls=self._download_base_urls,
                probe=self._download_probe,
                cache_ttl=self._download_cache_ttl,
            )
            if not url:
                logger.error(
                    "【123多盘STRM】换取下载地址失败（所有换链通道被风控或不可用），"
                    "请查看插件日志确认 123 账号状态"
                )
                return None
            logger.debug(f"【123多盘STRM】获取下载地址成功: {url}")
            return url
        except Exception as e:
            logger.error(f"【123多盘STRM】获取下载地址失败: {e}")
            return None

    def _pick_account(self, disk_name: str = ""):
        """
        选择用于换取下载地址的网盘账号

        :param disk_name: 优先匹配的网盘名称（空则取第一个可用账号）
        """
        accounts = getattr(self._api, "_accounts", []) or []
        if not accounts:
            return None
        if disk_name:
            for acc in accounts:
                if acc.name == disk_name:
                    return acc
            logger.warn(f"【123多盘STRM】未找到网盘 {disk_name}，使用默认账号")
        return accounts[0]

    # ==================== 分享票播放 ====================

    def _resolve_share_ticket_url(
        self,
        name: str,
        size: int,
        md5: str,
        user_agent: str,
        disk_name: str = "",
    ) -> Optional[str]:
        """
        分享票播放：md5 -> 分享转存记录 -> shareKey/FileId -> 分享下载票

        流程：
        1. 按 etag（md5）+ size 反查分享转存记录（分享重建后重新转存的
           记录优先，因为新记录带新 FileId）
        2. 老记录缺 FileId/S3KeyFlag 时，用 find_share_file 按 rel_path
           实时定位分享内文件并回填数据库
        3. 分享票换票 + 210 解析（缓存换票结果，过期按票 t 参数自动重换）

        :return: 最终边缘下载 URL；失败返回 None（原因已记日志）
        """
        records = self._find_share_records(md5, size)
        if not records:
            logger.warn(
                f"【123多盘STRM】分享票模式：文件 {name} 未找到分享转存记录"
                "（仅通过分享转存入库的文件可用分享票播放）"
            )
            return None
        account = self._pick_account(disk_name)
        if not account:
            logger.error("【123多盘STRM】没有可用的网盘账号，无法换取分享下载票")
            return None
        token = getattr(account.client, "token", "") or ""
        for task, rec in records:
            try:
                file_id = rec.get("file_id") or ""
                s3_flag = rec.get("share_s3_key_flag") or ""
                if not file_id or not s3_flag:
                    # 老版本转存记录：实时定位分享内文件并回填
                    sf = task.find_share_file(rec.get("rel_path") or "")
                    if not sf:
                        logger.warn(
                            f"【123多盘STRM】分享 {task.name} 内已找不到"
                            f" {rec.get('rel_path')}（分享可能已失效/已重建），跳过"
                        )
                        continue
                    file_id, s3_flag = sf.file_id, sf.s3_key_flag
                    try:
                        task._db.set_file_ids(
                            rec.get("file_key"), file_id, s3_flag
                        )
                    except Exception:
                        pass
                url = get_share_download_url(
                    token=token,
                    share_key=task.share_key,
                    share_pwd=task.share_pwd,
                    file_id=file_id,
                    s3_key_flag=s3_flag,
                    etag=md5,
                    size=size,
                    user_agent=user_agent,
                    cache=self._share_ticket_cache,
                )
                if url:
                    logger.debug(
                        f"【123多盘STRM】分享票播放地址: {url[:200]}"
                    )
                    return url
                logger.warn(
                    f"【123多盘STRM】分享 {task.name} 换票失败，尝试其他分享记录"
                )
            except Exception as e:
                logger.warn(
                    f"【123多盘STRM】分享 {task.name} 分享票换取异常: {e}"
                )
        return None

    def _find_share_records(
        self, md5: str, size: int
    ) -> List[Tuple[Any, Dict]]:
        """
        反查分享转存记录（跨任务合并，优先带 FileId 的最新记录）

        :return: [(ShareSync, record_dict), ...] 按可用性排序
        """
        result: List[Tuple[Any, Dict]] = []
        for task in self._shares:
            try:
                for rec in task._db.find_by_etag(md5, size):
                    result.append((task, rec))
            except Exception as e:
                logger.warn(f"【123多盘STRM】查询分享记录失败（{task.name}）: {e}")
        # 自分享目录记录（自己分享自己的文件，分享票播放）
        if self._self_share is not None:
            try:
                for rec in self._self_share.find_by_etag(md5, size):
                    result.append(
                        (
                            SimpleNamespace(
                                share_key=rec.get("share_key") or "",
                                share_pwd=rec.get("share_pwd") or "",
                                name=rec.get("dir_path") or "自分享",
                                find_share_file=lambda *a: None,
                            ),
                            rec,
                        )
                    )
            except Exception as e:
                logger.warn(f"【123多盘STRM】查询自分享记录失败: {e}")
        result.sort(
            key=lambda t: (
                1 if t[1].get("file_id") and t[1].get("share_s3_key_flag") else 0,
                t[1].get("created_at") or "",
            ),
            reverse=True,
        )
        return result

    # ==================== 全量同步 ====================

    def full_sync(
        self, paths_text: str, overwrite: bool = False
    ) -> Dict:
        """
        全量扫描配置的网盘目录并生成 STRM 文件（同步执行）

        与后台同步共用防重入锁：已有任务在运行时直接返回失败统计。

        :param paths_text: 路径映射配置，每行「本地STRM目录#网盘媒体库目录」
        :param overwrite: True 覆盖已存在的 STRM 文件
        :return: 统计结果 {"ok": n, "skip": n, "fail": n, "errors": [...], "paths": [...]}
        """
        if not self._sync_lock.acquire(blocking=False):
            return {
                "ok": 0,
                "skip": 0,
                "fail": 0,
                "errors": ["已有全量同步任务正在进行，请稍后再试"],
                "paths": [],
            }
        try:
            return self._full_sync_unlocked(paths_text, overwrite)
        finally:
            self._sync_lock.release()

    def _full_sync_unlocked(
        self, paths_text: str, overwrite: bool = False
    ) -> Dict:
        """
        全量同步核心实现（调用方须持有 _sync_lock）
        """
        result: Dict = {"ok": 0, "skip": 0, "fail": 0, "errors": [], "paths": []}
        mappings = self.parse_mappings(paths_text)
        if not mappings:
            result["errors"].append("未配置全量同步目录")
            return result
        for local_dir, pan_dir in mappings:
            root = self._api.get_item(Path(pan_dir))
            if not root or root.type != "dir":
                result["fail"] += 1
                result["errors"].append(f"网盘目录不存在或不可访问: {pan_dir}")
                continue
            logger.info(f"【123多盘STRM】开始全量同步: {pan_dir} -> {local_dir}")
            try:
                for item in self._walk_files(root):
                    if not self.is_media_file(item.name):
                        continue
                    rel_path = self._relative_path(item.path, pan_dir)
                    if not rel_path:
                        continue
                    strm_path = (
                        Path(local_dir)
                        / rel_path.parent
                        / (rel_path.stem + ".strm")
                    )
                    if strm_path.exists() and not overwrite:
                        result["skip"] += 1
                        continue
                    info = self._extract_file_info(item)
                    if not info:
                        result["fail"] += 1
                        result["errors"].append(f"文件信息缺失: {item.path}")
                        continue
                    url = self.build_strm_url(
                        name=info["name"],
                        size=info["size"],
                        md5=info["md5"],
                        s3_key_flag=info["s3_key_flag"],
                        disk_name=self._disk_of(item.path),
                    )
                    if not url:
                        result["fail"] += 1
                        break
                    if self._write_strm(strm_path, url):
                        result["ok"] += 1
                        result["paths"].append(str(strm_path))
                    else:
                        result["fail"] += 1
                        result["errors"].append(f"写入失败: {strm_path}")
            except Exception as e:
                result["fail"] += 1
                result["errors"].append(f"同步 {pan_dir} 失败: {e}")
        logger.info(
            f"【123多盘STRM】全量同步完成: 生成 {result['ok']} 个，"
            f"跳过 {result['skip']} 个，失败 {result['fail']} 个"
        )
        return result

    def start_full_sync(
        self,
        paths_text: str,
        overwrite: bool = False,
        on_done: Optional[Callable[[Dict], None]] = None,
    ) -> bool:
        """
        后台异步全量同步：立即返回，同步在守护线程中默默执行

        :param paths_text: 路径映射配置
        :param overwrite: True 覆盖已存在的 STRM 文件
        :param on_done: 同步完成后回调（参数为结果 dict），在后台线程中调用
        :return: True 已启动后台同步；False 已有同步任务在运行
        """
        if not self._sync_lock.acquire(blocking=False):
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
            target=self._sync_worker,
            args=(paths_text, overwrite),
            daemon=True,
            name="P123MultiStrmSync",
        ).start()
        return True

    def _sync_worker(self, paths_text: str, overwrite: bool):
        """
        后台同步工作线程（持有 _sync_lock）
        """
        self._sync_running = True
        self._sync_last_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = self._full_sync_unlocked(paths_text, overwrite)
            self._sync_last_result = result
            # 完成后回调（弱引用回调可能已被回收）
            cb = self._on_done_ref() if self._on_done_ref else None
            if cb:
                try:
                    cb(result)
                except Exception as e:
                    logger.warn(f"【123多盘STRM】同步完成回调执行失败: {e}")
        except Exception as e:
            self._sync_last_result = {
                "ok": 0,
                "skip": 0,
                "fail": 1,
                "errors": [f"后台同步异常: {e}"],
                "paths": [],
            }
            logger.error(f"【123多盘STRM】后台同步异常: {e}")
        finally:
            self._sync_running = False
            self._sync_lock.release()

    def sync_status(self) -> Dict:
        """
        后台同步状态快照

        :return: {"running": bool, "last_time": str|None,
                  "last_result": dict|None}
        """
        return {
            "running": self._sync_running,
            "last_time": self._sync_last_time,
            "last_result": self._sync_last_result,
        }

    def _walk_files(self, dir_item: FileItem) -> Iterable[FileItem]:
        """
        递归遍历目录，产出所有媒体文件（跳过蓝光原盘结构目录）

        :param dir_item: 起始目录项
        """
        try:
            children = self._api.list(dir_item) or []
        except Exception as e:
            logger.warn(f"【123多盘STRM】列出目录失败 {dir_item.path}: {e}")
            return
        for child in children:
            if child.type == "dir":
                if child.name.upper() in BLURAY_DIR_NAMES:
                    continue
                yield from self._walk_files(child)
            elif child.type == "file":
                yield child

    # ==================== 监控整理 ====================

    def handle_transfer_complete(
        self,
        target_item: FileItem,
        target_diritem_path: str,
        paths_text: str,
    ) -> Optional[Path]:
        """
        处理 MoviePilot 转移完成事件，为入库文件生成 STRM

        :param target_item: 转移后的目标文件项（须含 pickcode）
        :param target_diritem_path: 转移目标目录路径
        :param paths_text: 路径映射配置（本地STRM目录#网盘媒体库目录）
        :return: 生成的 STRM 文件路径，未生成返回 None
        """
        if not target_item or not target_item.path:
            return None
        # 仅处理媒体文件
        if not self.is_media_file(target_item.name or ""):
            return None
        info = self._extract_file_info(target_item)
        if not info:
            logger.warn(
                f"【123多盘STRM】{target_item.path} 缺少网盘文件信息，无法生成 STRM"
            )
            return None
        # 蓝光原盘不支持 STRM
        if self._is_bluray_dir(target_diritem_path):
            logger.warning(
                f"【123多盘STRM】{target_item.path} 为蓝光原盘目录，跳过"
            )
            return None
        # 匹配本地目录映射
        local_dir, pan_dir = self.match_media_path(
            target_item.path, self.parse_mappings(paths_text)
        )
        if not local_dir:
            logger.debug(
                f"【123多盘STRM】{target_item.path} 未匹配到 STRM 输出目录，跳过"
            )
            return None
        rel_path = self._relative_path(target_item.path, pan_dir)
        if not rel_path:
            return None
        strm_path = Path(local_dir) / rel_path.parent / (rel_path.stem + ".strm")
        url = self.build_strm_url(
            name=info["name"],
            size=info["size"],
            md5=info["md5"],
            s3_key_flag=info["s3_key_flag"],
            disk_name=self._disk_of(target_item.path),
        )
        if not url:
            return None
        if self._write_strm(strm_path, url):
            logger.info(f"【123多盘STRM】生成 STRM 文件: {strm_path}")
            return strm_path
        return None

    @staticmethod
    def _is_bluray_dir(dir_path: str) -> bool:
        """
        判断目录是否为蓝光原盘结构（目录名或上级目录名为 BDMV/CERTIFICATE 等）
        """
        if not dir_path:
            return False
        name = Path(str(dir_path).replace("\\", "/")).name.upper()
        return name in BLURAY_DIR_NAMES

    # ==================== 路径匹配 ====================

    @staticmethod
    def parse_mappings(text: str) -> List[Tuple[str, str]]:
        """
        解析路径映射配置：每行「本地STRM目录#网盘媒体库目录」

        :param text: 配置文本（可多行）
        :return: [(本地目录, 网盘目录), ...]
        """
        mappings: List[Tuple[str, str]] = []
        for line in str(text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "#" not in line:
                logger.warn(f"【123多盘STRM】忽略无效路径映射行: {line}")
                continue
            local_dir, _, pan_dir = line.partition("#")
            local_dir = local_dir.strip().strip("'\"")
            pan_dir = pan_dir.strip().strip("'\"")
            if not local_dir or not pan_dir:
                logger.warn(f"【123多盘STRM】忽略无效路径映射行: {line}")
                continue
            mappings.append((local_dir, pan_dir))
        return mappings

    @classmethod
    def match_media_path(
        cls, path: str, mappings: List[Tuple[str, str]]
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        匹配路径对应的本地输出目录与网盘目录

        :param path: 网盘文件路径（带盘前缀）
        :param mappings: parse_mappings 结果
        :return: (本地目录, 网盘目录)，未匹配返回 (None, None)
        """
        norm = str(path).replace("\\", "/").rstrip("/")
        best: Optional[Tuple[str, str]] = None
        for local_dir, pan_dir in mappings:
            pan_norm = str(pan_dir).replace("\\", "/").rstrip("/")
            if not pan_norm:
                continue
            if norm == pan_norm or norm.startswith(pan_norm + "/"):
                # 取最长的匹配前缀（配置多个嵌套目录时精确匹配）
                if best is None or len(pan_norm) > len(best[1]):
                    best = (local_dir, pan_dir)
        if best:
            return best
        return None, None

    # ==================== 文件工具 ====================

    @staticmethod
    def _extract_file_info(fileitem: FileItem) -> Optional[Dict]:
        """
        从 FileItem.pickcode 提取 123 原始文件信息

        :return: {"name", "size", "md5", "s3_key_flag"}，缺失返回 None
        """
        if not fileitem or not fileitem.pickcode:
            return None
        try:
            data = ast.literal_eval(fileitem.pickcode)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        name = data.get("FileName") or fileitem.name
        size = data.get("Size")
        md5 = data.get("Etag")
        s3_key_flag = data.get("S3KeyFlag")
        if not name or size is None or not md5 or not s3_key_flag:
            return None
        return {
            "name": name,
            "size": int(size),
            "md5": md5,
            "s3_key_flag": s3_key_flag,
        }

    @classmethod
    def _relative_path(
        cls, path: str, base: str
    ) -> Optional[PurePosixPath]:
        """
        计算网盘路径相对于映射目录的相对路径
        """
        try:
            return PurePosixPath(str(path).replace("\\", "/")).relative_to(
                PurePosixPath(str(base).replace("\\", "/").rstrip("/"))
            )
        except ValueError:
            return None

    @classmethod
    def _disk_of(cls, path: str) -> str:
        """
        从虚拟路径提取网盘名称（首段）
        """
        parts = str(path).replace("\\", "/").strip("/").split("/", 1)
        return parts[0] if parts and parts[0] else ""

    def is_media_file(self, name: str) -> bool:
        """
        判断是否为媒体文件（按扩展名）
        """
        if not name:
            return False
        ext = Path(name).suffix.lstrip(".").lower()
        return ext in self._media_exts

    @staticmethod
    def _write_strm(strm_path: Path, url: str) -> bool:
        """
        写入 STRM 文件
        """
        try:
            strm_path.parent.mkdir(parents=True, exist_ok=True)
            with open(strm_path, "w", encoding="utf-8") as f:
                f.write(url)
            return True
        except Exception as e:
            logger.error(f"【123多盘STRM】写入 STRM 文件失败 {strm_path}: {e}")
            return False
