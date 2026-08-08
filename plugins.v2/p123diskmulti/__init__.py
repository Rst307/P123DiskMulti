import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import schemas
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.mediaserver import MediaServerHelper
from app.helper.storage import StorageHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem, RefreshMediaItem, StorageOperSelectionEventData
from app.schemas.types import ChainEventType, EventType
from app.utils.string import StringUtils

from .organize import DEFAULT_ORGANIZE_CRON, OrganizeRunner
from .p123_api import DiskAccount, P123MultiApi
from .share import ShareSync
from .strm import DEFAULT_MEDIA_EXTS, StrmHelper

# 默认网盘名称（旧版单盘配置迁移时使用）
LEGACY_DISK_NAME = "盘1"
# 默认全量同步 cron（每 7 小时）
DEFAULT_STRM_CRON = "0 */7 * * *"
# 默认分享同步 cron（每 6 小时）
DEFAULT_SHARE_CRON = "0 */6 * * *"


class P123DiskMulti(_PluginBase):
    """
    123云盘多盘合并储存插件：多个123网盘合并为一个存储空间，
    支持网盘间文件互传、空间不足自动切换网盘、合并空间显示
    """

    # 插件名称
    plugin_name = "123云盘多盘合并"
    # 插件描述
    plugin_desc = "多盘合并使用：多个123网盘合并为一个存储空间，支持网盘间互传、空间不足自动切换、合并空间显示。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/DDSRem-Dev/MoviePilot-Plugins/main/icons/P123Disk.png"
    # 插件版本
    plugin_version = "1.4.2"
    # 插件作者
    plugin_author = "Rst307"
    # 作者主页
    author_url = "https://github.com/Rst307"
    # 插件配置项ID前缀
    plugin_config_prefix = "p123multi_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 是否启用
    _enabled = False
    _auto_switch = True
    _reserve_size = 0
    _api: Optional[P123MultiApi] = None
    _disk_name = "123云盘"

    def __init__(self):
        super().__init__()
        self._disk_name = "123云盘"
        # STRM 助手与定时任务
        self._strm: Optional[StrmHelper] = None
        self._scheduler: Optional[BackgroundScheduler] = None
        # 下载换链域名（高级）：每行一个，留空用内置默认
        self._download_base_urls: Optional[List[str]] = None
        # 下载票探测 + 通道风控自动切换域名
        self._download_probe = True
        # 分享增量同步任务
        self._shares: List[ShareSync] = []
        # 定期目录整理（调用 MoviePilot 整理链）
        self._organize: Optional[OrganizeRunner] = None

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        self._api = None
        self._strm = None
        self._organize = None
        self.stop_service()
        if config:
            self._enabled = config.get("enabled", False)
            self._auto_switch = config.get("auto_switch", True)
            try:
                self._reserve_size = (
                    float(config.get("reserve_size") or 0) * 1024 ** 3
                )
            except Exception:
                self._reserve_size = 0

            # STRM 功能配置
            self._strm_enabled = config.get("strm_enabled", False)
            self._moviepilot_address = config.get("moviepilot_address") or ""
            self._transfer_monitor_paths = (
                config.get("transfer_monitor_paths") or ""
            )
            self._full_sync_paths = config.get("full_sync_paths") or ""
            self._full_sync_cron = config.get("full_sync_cron") or DEFAULT_STRM_CRON
            self._full_sync_overwrite = config.get("full_sync_overwrite", False)
            self._refresh_mediaserver = config.get("refresh_mediaserver", False)
            self._mediaserver_names = config.get("mediaserver_names") or ""

            # 下载换链域名（高级）：每行一个，留空用内置默认
            self._download_base_urls = [
                l.strip()
                for l in str(config.get("download_base_urls") or "").splitlines()
                if l.strip()
            ] or None
            self._download_probe = config.get("download_probe", True)

            # 分享增量同步配置
            self._share_enabled = config.get("share_enabled", False)
            self._shares_text = config.get("shares_text") or ""
            self._share_cron = config.get("share_cron") or DEFAULT_SHARE_CRON

            # 定期目录整理配置
            self._organize_enabled = config.get("organize_enabled", False)
            self._organize_paths = config.get("organize_paths") or ""
            self._organize_cron = config.get("organize_cron") or DEFAULT_ORGANIZE_CRON
            self._organize_delete_better = config.get(
                "organize_delete_better", True
            )

            # 解析网盘账号列表
            accounts = self._parse_disks(config)

            if self._enabled:
                # 添加云盘存储配置（不存在时）
                storage_helper = StorageHelper()
                storages = storage_helper.get_storagies()
                if not any(
                    s.type == self._disk_name and s.name == self._disk_name
                    for s in storages
                ):
                    storage_helper.add_storage(
                        storage=self._disk_name, name=self._disk_name, conf={}
                    )

                if accounts:
                    self._api = P123MultiApi(
                        disks=accounts,
                        disk_name=self._disk_name,
                        reserve_size=self._reserve_size,
                        auto_switch=self._auto_switch,
                    )
                    logger.info(
                        f"【123多盘】插件已启用，共 {len(accounts)} 个网盘：{', '.join(a.name for a in accounts)}"
                    )
                    # 初始化 STRM 助手（多盘）
                    self._init_strm()
                    # 初始化分享增量同步
                    self._init_shares()
                    # 初始化定期目录整理
                    self._init_organize()
                else:
                    logger.warn("【123多盘】未配置任何网盘账号，请填写网盘账号列表")
            else:
                logger.info("【123多盘】插件未启用")

    def _init_strm(self):
        """
        初始化 STRM 助手与定时同步任务
        """
        if not self._strm_enabled:
            logger.info("【123多盘】STRM 功能未启用")
            return
        self._strm = StrmHelper(
            api=self._api,
            moviepilot_address=self._moviepilot_address,
            media_exts=DEFAULT_MEDIA_EXTS,
            download_base_urls=self._download_base_urls,
            download_probe=self._download_probe,
        )
        if self._full_sync_paths and self._full_sync_cron:
            try:
                trigger = CronTrigger.from_crontab(self._full_sync_cron)
                self._scheduler = BackgroundScheduler(
                    timezone=settings.TZ
                )
                self._scheduler.add_job(
                    func=self._scheduled_strm_sync,
                    trigger=trigger,
                    id="p123diskmulti_strm_sync",
                    name="123多盘全量同步STRM",
                    max_instances=1,
                    coalesce=True,
                )
                self._scheduler.start()
                logger.info(
                    f"【123多盘】STRM 定时全量同步已启动: {self._full_sync_cron}"
                )
            except Exception as e:
                logger.error(f"【123多盘】STRM 定时任务启动失败: {e}")
                self._scheduler = None

    def _scheduled_strm_sync(self):
        """
        定时全量同步 STRM（后台执行，不与手动同步并发）
        """
        if not self._strm:
            return
        started = self._strm.start_full_sync(
            self._full_sync_paths or "",
            overwrite=self._full_sync_overwrite,
            on_done=self._strm_sync_done,
        )
        if not started:
            logger.info("【123多盘】定时同步触发时已有同步任务在运行，跳过本次")

    def _init_organize(self):
        """
        初始化定期目录整理与定时任务
        """
        if not self._organize_enabled:
            logger.info("【123多盘】定期目录整理未启用")
            return
        self._organize = OrganizeRunner(api=self._api)
        if self._organize_paths and self._organize_cron:
            try:
                trigger = CronTrigger.from_crontab(self._organize_cron)
                if not self._scheduler:
                    self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                    self._scheduler.start()
                self._scheduler.add_job(
                    func=self._scheduled_organize,
                    trigger=trigger,
                    id="p123diskmulti_organize",
                    name="123多盘定期目录整理",
                    max_instances=1,
                    coalesce=True,
                )
                logger.info(
                    f"【123多盘】定期目录整理已启动: {self._organize_cron}"
                )
            except Exception as e:
                logger.error(f"【123多盘】定期整理定时任务启动失败: {e}")

    def _scheduled_organize(self):
        """
        定时目录整理（后台执行，不与手动整理并发）
        """
        if not self._organize:
            return
        started = self._organize.start_organize(self._organize_paths or "")
        if not started:
            logger.info("【123多盘】定时整理触发时已有整理任务在运行，跳过本次")

    @eventmanager.register(EventType.TransferFailed)
    def organize_transfer_failed(self, event: Event):
        """
        监听整理失败事件：源文件在 123 盘且失败原因为目标已有更好文件时，
        自动删除网盘上的低版本源文件（移入回收站）
        """
        if not self._enabled or not self._organize:
            return
        if not self._organize_delete_better:
            return
        try:
            self._organize.handle_transfer_failed(event.event_data)
        except Exception as e:
            logger.error(f"【123多盘】整理失败自动清理异常: {e}")

    def _init_shares(self):
        """
        初始化分享增量同步任务与定时服务
        """
        self._shares = []
        if not self._share_enabled:
            logger.info("【123多盘】分享增量同步未启用")
            return
        db_path = self.get_data_path() / "p123sharesync.sqlite3"
        for conf in self._parse_shares(self._shares_text):
            try:
                task = ShareSync(
                    api=self._api,
                    task_id=conf["task_id"],
                    name=conf["name"],
                    share_key=conf["share_key"],
                    share_pwd=conf["share_pwd"],
                    target_vpath=conf["target_path"],
                    db_path=db_path,
                    auto_switch=self._auto_switch,
                    reserve_size=self._reserve_size,
                    note=conf.get("note") or "",
                )
                self._shares.append(task)
                logger.info(
                    f"【123多盘】分享任务已加载: {conf['name']} -> {conf['target_path']}"
                )
            except Exception as e:
                logger.error(f"【123多盘】分享任务 {conf['name']} 加载失败: {e}")
        if not self._shares:
            return
        # 定时增量转存（复用 STRM 的调度器，仅首次创建）
        if self._share_cron:
            try:
                trigger = CronTrigger.from_crontab(self._share_cron)
                if not self._scheduler:
                    self._scheduler = BackgroundScheduler(
                        timezone=settings.TZ
                    )
                    self._scheduler.start()
                self._scheduler.add_job(
                    func=self._scheduled_share_sync,
                    trigger=trigger,
                    id="p123diskmulti_share_sync",
                    name="123多盘分享增量同步",
                    max_instances=1,
                    coalesce=True,
                )
                logger.info(
                    f"【123多盘】分享定时增量转存已启动: {self._share_cron}"
                )
            except Exception as e:
                logger.error(f"【123多盘】分享定时任务启动失败: {e}")

    def _scheduled_share_sync(self):
        """
        定时增量转存所有分享（后台执行）
        """
        for task in self._shares:
            if not task.start_sync():
                logger.info(f"【123多盘】分享 {task.name} 已有转存任务在运行，跳过本次")

    @staticmethod
    def _parse_shares(shares_text: str) -> List[Dict[str, Any]]:
        """
        解析分享同步配置

        支持两种格式：
        1. 每行一条：名称,分享链接或分享码,提取码,目标目录,注释（可选，提取码可为空：名称,链接,,目标目录）
        2. JSON 数组：[{"name":..., "share_key":..., "share_pwd":..., "target_path":..., "note":...}]
        """
        result: List[Dict[str, Any]] = []
        raw = shares_text
        if isinstance(raw, (list, dict)):
            items = raw if isinstance(raw, list) else [raw]
        else:
            text = str(raw or "").strip()
            if text.startswith("["):
                try:
                    items = json.loads(text)
                except Exception:
                    items = []
            else:
                items = [
                    line
                    for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
        used_ids = set()
        for index, item in enumerate(items):
            if isinstance(item, dict):
                name = str(
                    item.get("name") or item.get("id") or f"分享{index + 1}"
                ).strip()
                share_key = str(
                    item.get("share_key")
                    or item.get("share_url")
                    or item.get("share_code")
                    or ""
                ).strip()
                share_pwd = str(
                    item.get("share_pwd") or item.get("share_password") or ""
                ).strip()
                target_path = str(
                    item.get("target_path") or item.get("target") or ""
                ).strip()
                note = str(
                    item.get("note")
                    or item.get("comment")
                    or item.get("remark")
                    or ""
                ).strip()
            else:
                parts = [part.strip() for part in str(item).split(",", 4)]
                if len(parts) < 4:
                    logger.warn(f"【123多盘】忽略无效分享配置行: {item}")
                    continue
                name, share_key, share_pwd, target_path = parts[:4]
                note = parts[4] if len(parts) > 4 else ""
            if not name or not share_key or not target_path:
                logger.warn(f"【123多盘】忽略不完整的分享配置: {item}")
                continue
            # 名称去重，避免身份命名空间冲突
            task_id = name
            if task_id in used_ids:
                task_id = f"{name}-{index + 1}"
            used_ids.add(task_id)
            result.append(
                {
                    "task_id": task_id,
                    "name": name,
                    "share_key": share_key,
                    "share_pwd": share_pwd,
                    "target_path": target_path.rstrip("/"),
                    "note": note,
                }
            )
        return result

    def _find_share(self, name: str = "") -> Optional[ShareSync]:
        """
        按名称或任务 ID 查找分享任务
        """
        if not name:
            return None
        for task in self._shares:
            if task.name == name or task.task_id == name:
                return task
        return None

    @staticmethod
    def _parse_disks(config: dict) -> List[DiskAccount]:
        """
        解析网盘账号配置

        支持格式：
        1. disks_text 每行一个：网盘名称,手机号,密码
        2. disks 字段（JSON数组）：[{"name": "...", "passport": "...", "password": "..."}]
        3. 旧版单盘配置：passport + password（自动迁移为单个网盘）
        """
        accounts: List[DiskAccount] = []
        seen_names = set()

        def _add(name: str, passport: str, password: str):
            name = str(name or "").strip().replace("/", "_").replace(",", "_")
            passport = str(passport or "").strip()
            password = str(password or "").strip()
            if not name or not passport or not password:
                logger.warn(f"【123多盘】忽略无效网盘配置: 名称={name or '空'} 手机号={passport or '空'}")
                return
            if name in seen_names:
                logger.warn(f"【123多盘】网盘名称 {name} 重复，已忽略（名称需唯一）")
                return
            seen_names.add(name)
            accounts.append(DiskAccount(name=name, passport=passport, password=password))

        # 1. 新版列表配置（JSON）
        disks = config.get("disks")
        if isinstance(disks, list) and disks:
            for disk in disks:
                if isinstance(disk, dict):
                    _add(
                        disk.get("name"),
                        disk.get("passport"),
                        disk.get("password"),
                    )

        # 2. 文本框配置（每行：名称,手机号,密码）
        disks_text = config.get("disks_text") or ""
        if not accounts and disks_text:
            text = disks_text.strip()
            if text.startswith("["):
                # 兼容 JSON 数组输入
                try:
                    disks = json.loads(text)
                    for disk in disks:
                        if isinstance(disk, dict):
                            _add(
                                disk.get("name"),
                                disk.get("passport"),
                                disk.get("password"),
                            )
                except Exception as e:
                    logger.error(f"【123多盘】解析 JSON 配置失败: {e}")
            else:
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        _add(parts[0], parts[1], parts[2])
                    else:
                        logger.warn(f"【123多盘】忽略无效配置行: {line}")

        # 3. 旧版单盘配置迁移
        if not accounts and config.get("passport") and config.get("password"):
            logger.info("【123多盘】检测到旧版单盘配置，自动迁移")
            _add(LEGACY_DISK_NAME, config.get("passport"), config.get("password"))

        return accounts

    def get_state(self) -> bool:
        """
        返回插件启用状态

        :return: True 表示插件已启用
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        返回插件远程命令列表，本插件无远程命令

        :return: None
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        返回插件 API 端点列表
        """
        return [
            {
                "path": "/usage",
                "endpoint": self.api_usage,
                "auth": "bear",
                "methods": ["GET"],
                "summary": "获取所有网盘空间使用情况",
                "description": "获取所有网盘合并及明细空间使用情况",
            },
            {
                "path": "/test",
                "endpoint": self.api_test,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "测试所有网盘连接",
                "description": "测试所有网盘账号的连接状态",
            },
            {
                "path": "/balance",
                "endpoint": self.api_balance,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "一键均衡各网盘空间",
                "description": "将空间紧张网盘中的文件自动移动到剩余空间最大的网盘",
            },
            {
                "path": "/redirect_url",
                "endpoint": self.api_redirect_url,
                "auth": "apikey",
                "methods": ["GET", "HEAD"],
                "summary": "123云盘302跳转（STRM播放）",
                "description": "根据文件标识实时换取 123 下载地址并 302 重定向，供 Emby 播放 STRM 使用（HEAD 探测也支持）",
            },
            {
                "path": "/strm_sync",
                "endpoint": self.api_strm_sync,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "全量同步生成STRM（后台）",
                "description": "后台扫描所有网盘的媒体目录并生成 STRM 文件，立即返回",
            },
            {
                "path": "/strm_status",
                "endpoint": self.api_strm_status,
                "auth": "bear",
                "methods": ["GET"],
                "summary": "查询后台STRM同步状态",
                "description": "返回当前是否在同步、上次同步时间与结果",
            },
            {
                "path": "/share/check",
                "endpoint": self.api_share_check,
                "auth": "bear",
                "methods": ["GET"],
                "summary": "检查分享内容",
                "description": "检查指定 123 分享的可访问性与文件统计（参数 name=分享名称）",
            },
            {
                "path": "/share/sync",
                "endpoint": self.api_share_sync,
                "auth": "bear",
                "methods": ["GET"],
                "summary": "增量转存分享（后台）",
                "description": "后台服务器端直传转存指定分享的新文件（参数 name=分享名称）",
            },
            {
                "path": "/share/run",
                "endpoint": self.api_share_run,
                "auth": "bear",
                "methods": ["GET"],
                "summary": "立刻检测转存分享（后台）",
                "description": "一键完成分享内容检测（可访问性/文件统计）+ 增量转存新文件；参数 name=分享名称，留空则全部任务",
            },
            {
                "path": "/share/status",
                "endpoint": self.api_share_status,
                "auth": "bear",
                "methods": ["GET"],
                "summary": "查询分享任务状态",
                "description": "返回所有分享任务的运行状态、已转存数量与上次结果",
            },
            {
                "path": "/organize",
                "endpoint": self.api_organize,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "定期目录整理（后台）",
                "description": "扫描指定 123 盘目录的媒体文件并提交到 MoviePilot 整理队列，立即返回",
            },
            {
                "path": "/organize_status",
                "endpoint": self.api_organize_status,
                "auth": "bear",
                "methods": ["GET"],
                "summary": "查询后台目录整理状态",
                "description": "返回当前是否在整理、上次整理时间与结果",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 动态获取 MoviePilot 已配置的媒体服务器（供 STRM 刷新选择）
        server_items: List[str] = []
        try:
            services = MediaServerHelper().get_services()
            server_items = sorted(services.keys())
        except Exception as e:
            logger.warn(f"【123多盘】获取媒体服务器列表失败: {e}")
        # 合并配置中已保存的名称（防止服务器被删除后配置值丢失显示）
        current_names = self._mediaserver_names
        if isinstance(current_names, list):
            current_names = [str(x) for x in current_names]
        else:
            current_names = [
                x.strip()
                for x in str(current_names or "").split(",")
                if x.strip()
            ]
        server_items = sorted(set(server_items) | set(current_names))
        if not server_items:
            server_items_hint = "未检测到媒体服务器，请先在「设置→媒体服务器」中配置"
        else:
            server_items_hint = "从 MoviePilot 已配置的媒体服务器中选择，留空则刷新所有"
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            # 使用说明
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "compact",
                                        },
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {
                                                    "class": "text-subtitle-2 font-weight-bold mb-1"
                                                },
                                                "text": "💡 插件使用说明",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 多个123网盘将合并为一个存储「123云盘」，根目录下每个网盘对应一个文件夹",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 下载/媒体库路径请填写「/网盘名称/目录」，如 /我的盘A/电影",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 跨网盘移动/复制文件：在整理时选择不同网盘下的目标目录即可自动完成",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 开启「空间不足自动切换」后，目标网盘空间不足时文件将自动上传到其他网盘",
                                            },
                                        ],
                                    }
                                ],
                            },
                            # 基础设置
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "color": "primary",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_switch",
                                            "label": "空间不足自动切换网盘",
                                            "color": "primary",
                                            "hint": "上传时目标网盘空间不足，自动选择剩余空间最大的网盘",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "reserve_size",
                                            "label": "每个网盘预留空间（GB）",
                                            "type": "number",
                                            "hint": "剩余空间低于该值时视为空间不足，自动切换网盘",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"class": "my-2"},
                                    }
                                ],
                            },
                            # 网盘账号列表
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "div",
                                        "props": {
                                            "class": "text-subtitle-1 font-weight-bold mb-1"
                                        },
                                        "text": "📦 网盘账号列表",
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "disks_text",
                                            "rows": 5,
                                            "label": "网盘账号（每行一个）",
                                            "placeholder": "网盘名称,手机号,密码\n例如：\n我的盘A,13800138000,123456\n我的盘B,13900139000,abcdef",
                                            "hint": "每行一个网盘：网盘名称,手机号,密码。网盘名称是「123云盘」根目录下的文件夹名，需唯一，修改名称会影响已配置的目录路径",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            # STRM 功能
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"class": "my-2"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "div",
                                        "props": {
                                            "class": "text-subtitle-1 font-weight-bold mb-1"
                                        },
                                        "text": "🎬 STRM 功能（多盘支持）",
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "strm_enabled",
                                            "label": "启用 STRM 功能",
                                            "color": "primary",
                                            "hint": "生成 STRM 文件并支持 302 直链播放（所有网盘通用）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "moviepilot_address",
                                            "label": "MoviePilot 访问地址",
                                            "placeholder": "http://192.168.1.10:3000",
                                            "hint": "STRM 播放 URL 前缀，需能被 Emby 访问到（带端口）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "transfer_monitor_paths",
                                            "rows": 3,
                                            "label": "整理事件监控目录",
                                            "placeholder": "本地STRM目录#网盘媒体库目录\n例如：\n/volume1/strm/movies#/盘A/电影\n/volume1/strm/tv#/盘B/剧集",
                                            "hint": "监控 MoviePilot 整理入库事件，自动在本地生成 STRM 文件；网盘目录需带盘前缀",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "full_sync_paths",
                                            "rows": 3,
                                            "label": "全量同步目录",
                                            "placeholder": "本地STRM目录#网盘媒体库目录\n例如：\n/volume1/strm/movies#/盘A/电影\n/volume1/strm/tv#/盘B/剧集",
                                            "hint": "全量扫描所有网盘的媒体目录并生成 STRM 文件，可配置多行多盘",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "full_sync_cron",
                                            "label": "全量同步定时（cron）",
                                            "hint": "留空则不自动同步；默认每 7 小时",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "full_sync_overwrite",
                                            "label": "覆盖已有 STRM 文件",
                                            "color": "primary",
                                            "hint": "关闭时已存在的 STRM 文件将被跳过",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "refresh_mediaserver",
                                            "label": "生成后刷新媒体服务器",
                                            "color": "primary",
                                            "hint": "STRM 生成后自动刷新 Emby 等媒体库",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "download_probe",
                                            "label": "风控自动切换换链域名",
                                            "color": "primary",
                                            "hint": "换链后探测下载票有效性，被风控（403 50002/1010）时自动切换到另一换链通道，避免全库无法播放",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "download_base_urls",
                                            "rows": 2,
                                            "label": "下载换链域名（高级）",
                                            "placeholder": "https://api.123278.com/b",
                                            "hint": "每行一个，按优先级尝试；留空用内置默认（api.123278.com/b 优先，p123client 默认域名兜底）。仅当 123 更换域名或默认通道失效时需调整",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mediaserver_names",
                                            "label": "媒体服务器名称",
                                            "items": server_items,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": server_items_hint,
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "compact",
                                        },
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {
                                                    "class": "text-subtitle-2 font-weight-bold mb-1"
                                                },
                                                "text": "💡 STRM 使用说明",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 在 Emby 中添加本地媒体库指向「本地STRM目录」，播放时自动通过 302 直链从 123 网盘拉流",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• STRM URL 形如 /api/v1/plugin/P123DiskMulti/redirect_url?apikey=...&s3_key_flag=...，无需额外配置",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 全量同步适合存量文件，整理事件监控适合新入库文件，两者可同时开启",
                                            },
                                        ],
                                    }
                                ],
                            },
                            # 分享增量同步
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"class": "my-2"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "div",
                                        "props": {
                                            "class": "text-subtitle-1 font-weight-bold mb-1"
                                        },
                                        "text": "📤 分享增量同步（多盘支持）",
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "share_enabled",
                                            "label": "启用分享增量同步",
                                            "color": "primary",
                                            "hint": "定时检查分享内容，服务器端直传转存新文件（不占本地带宽）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "share_cron",
                                            "label": "定时增量转存（cron）",
                                            "hint": "留空则不自动转存；默认每 6 小时",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "shares_text",
                                            "rows": 4,
                                            "label": "分享任务列表（每行一个）",
                                            "placeholder": "名称,分享链接或分享码,提取码,目标目录,注释(可选)\n例如：\n东京教父,https://www.123pan.com/s/AbC123-DEF,1234,/盘A/分享/电影,东京教父1080p\n剧集分享,Sa7K8-QwEr,,/盘B/分享/剧集,某某剧第一季",
                                            "hint": "提取码与注释可为空（字段留空）；目标目录必须带网盘前缀；注释会显示在插件页卡片上，方便区分多个任务对应的剧/链接",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "compact",
                                        },
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {
                                                    "class": "text-subtitle-2 font-weight-bold mb-1"
                                                },
                                                "text": "💡 分享同步说明",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 转存为 123 服务器端直传（云端到云端），速度与本地网络无关，也不占磁盘空间",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 已转存文件记录在插件数据目录的 SQLite 中，同名文件增删后不会重复转存",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 转存中目标网盘空间不足（低于预留空间）时，自动切换到剩余空间最大的网盘继续转存（受「空间不足自动切换网盘」总开关控制），无需手动改配置",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 插件详情页可「检查内容」「⚡ 立刻检测转存」，转存在后台执行，页面不阻塞；立刻检测转存一键完成分享检测 + 增量转存",
                                            },
                                        ],
                                    }
                                ],
                            },
                            # 定期目录整理
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VDivider",
                                        "props": {"class": "my-2"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "div",
                                        "props": {
                                            "class": "text-subtitle-1 font-weight-bold mb-1"
                                        },
                                        "text": "🗂️ 定期目录整理（调用 MoviePilot 整理链）",
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "organize_enabled",
                                            "label": "启用定期目录整理",
                                            "color": "primary",
                                            "hint": "定期将指定 123 盘目录内的媒体文件提交到 MoviePilot 整理队列（重命名/刮削/进媒体库）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "organize_cron",
                                            "label": "定时整理（cron）",
                                            "hint": "留空则不自动整理；默认每天凌晨 3 点",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "organize_delete_better",
                                            "label": "整理失败自动删除网盘低版本文件",
                                            "color": "primary",
                                            "hint": "整理失败原因为「媒体库已有同名且质量更好」时，自动删除网盘上的源文件（移入回收站可恢复）",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "organize_paths",
                                            "rows": 3,
                                            "label": "整理目录（每行一个）",
                                            "placeholder": "/盘A/emby_raw\n/rst307/emby_raw\n/盘B/下载/剧集",
                                            "hint": "需带网盘前缀（如 /盘A/电影）；递归扫描目录内的视频文件并逐个提交整理，字幕等附加文件由 MoviePilot 整理链自动同步",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "compact",
                                        },
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {
                                                    "class": "text-subtitle-2 font-weight-bold mb-1"
                                                },
                                                "text": "💡 定期整理说明",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 整理完全调用 MoviePilot 自带整理链：重命名、刮削、进媒体库等规则由 MoviePilot 的转移目录/整理配置决定，请在 MoviePilot「目录同步」中为 123 盘目录配置好整理目标",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 文件提交到 MoviePilot 整理队列后台执行（不阻塞插件），已整理过的文件不会被重复整理（MoviePilot 自动去重）",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 同盘移动为 123 服务器端秒级操作；整理完成后 MoviePilot 会自动通知媒体服务器刷新",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 整理失败且原因「媒体库已有同名且质量更好」时，自动删除网盘上的低版本源文件（移入回收站，可恢复），释放空间",
                                            },
                                        ],
                                    }
                                ],
                            },
                            # 注意事项
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "density": "compact",
                                        },
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {
                                                    "class": "text-subtitle-2 font-weight-bold mb-1"
                                                },
                                                "text": "⚠️ 注意事项",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 保存后可在「插件详情」页查看各网盘空间使用情况（合并显示）",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 修改网盘名称后，该网盘下的文件将不再通过原路径访问，请谨慎修改",
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": "• 网盘间互传（下载再上传）会占用本地临时空间，文件越大耗时越长",
                                            },
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "auto_switch": True,
            "reserve_size": 10,
            "disks_text": "",
            # STRM 功能默认配置
            "strm_enabled": False,
            "moviepilot_address": "",
            "transfer_monitor_paths": "",
            "full_sync_paths": "",
            "full_sync_cron": "0 */7 * * *",
            "full_sync_overwrite": False,
            "refresh_mediaserver": False,
            "mediaserver_names": "",
            # 下载换链域名（高级）：留空用内置默认
            "download_base_urls": "",
            "download_probe": True,
            # 分享增量同步默认配置
            "share_enabled": False,
            "shares_text": "",
            "share_cron": "0 */6 * * *",
            # 定期目录整理默认配置
            "organize_enabled": False,
            "organize_paths": "",
            "organize_cron": "0 3 * * *",
            "organize_delete_better": True,
        }

    def get_page(self) -> List[dict]:
        """
        返回插件数据页面配置：空间使用情况（合并显示 + 各网盘明细）
        """
        if not self._api:
            return []
        try:
            details = self._api.usage_details()
        except Exception as e:
            logger.error(f"【123多盘】获取空间信息失败: {e}")
            return []

        disks = details.get("disks") or []
        total, used, available = (
            details.get("total", 0),
            details.get("used", 0),
            details.get("available", 0),
        )

        def _percent(_used: float, _total: float) -> int:
            return round(_used * 100 / _total) if _total else 0

        def _color(_pct: int) -> str:
            return "success" if _pct < 70 else ("warning" if _pct < 90 else "error")

        # 页面内容
        content = [
            {
                "component": "VRow",
                "content": [
                    # 顶部操作按钮
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "class": "d-flex align-center"},
                        "content": [
                            {
                                "component": "span",
                                "props": {"class": "text-h6 font-weight-bold me-4"},
                                "text": "🗄️ 123云盘 · 多盘合并空间",
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "tonal",
                                    "size": "small",
                                    "class": "mr-2",
                                    "prepend-icon": "mdi-refresh",
                                },
                                "text": "刷新",
                                "events": {
                                    "click": {
                                        "api": "plugin/P123DiskMulti/usage",
                                        "method": "get",
                                    },
                                },
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "warning",
                                    "variant": "tonal",
                                    "size": "small",
                                    "prepend-icon": "mdi-scale-balance",
                                },
                                "text": "一键均衡空间",
                                "events": {
                                    "click": {
                                        "api": "plugin/P123DiskMulti/balance",
                                        "method": "post",
                                    },
                                },
                            },
                        ],
                    },
                    # 合并空间卡片
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "div",
                                                "content": [
                                                    {
                                                        "component": "span",
                                                        "props": {
                                                            "class": "text-subtitle-1 font-weight-bold"
                                                        },
                                                        "text": f"合并总空间（{len(disks)} 个网盘）",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {
                                                            "class": "text-caption text-medium-emphasis mb-2"
                                                        },
                                                        "text": (
                                                            f"已用 {StringUtils.str_filesize(used)} / "
                                                            f"总 {StringUtils.str_filesize(total)} · "
                                                            f"剩余 {StringUtils.str_filesize(available)}"
                                                        ),
                                                    },
                                                    {
                                                        "component": "VProgressLinear",
                                                        "props": {
                                                            "model-value": _percent(used, total),
                                                            "color": _color(_percent(used, total)),
                                                            "height": 10,
                                                            "rounded": True,
                                                        },
                                                    },
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

        # 各网盘空间卡片
        disk_cards = []
        for disk in disks:
            disk_content = [
                {
                    "component": "div",
                    "content": [
                        {
                            "component": "span",
                            "props": {"class": "text-subtitle-1 font-weight-bold"},
                            "text": f"📀 {disk.get('name', '')}",
                        },
                        {
                            "component": "div",
                            "props": {
                                "class": "text-caption text-medium-emphasis mb-2"
                            },
                            "text": (
                                f"已用 {StringUtils.str_filesize(disk.get('used') or 0)} / "
                                f"总 {StringUtils.str_filesize(disk.get('total') or 0)} · "
                                f"剩余 {StringUtils.str_filesize(disk.get('available') or 0)}"
                            )
                            if disk.get("ok")
                            else disk.get("error", "获取空间信息失败"),
                        },
                    ],
                }
            ]
            if disk.get("ok"):
                disk_pct = _percent(disk.get("used") or 0, disk.get("total") or 0)
                disk_content.append(
                    {
                        "component": "VProgressLinear",
                        "props": {
                            "model-value": disk_pct,
                            "color": _color(disk_pct),
                            "height": 8,
                            "rounded": True,
                        },
                    }
                )
            else:
                disk_content.append(
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "error",
                            "variant": "tonal",
                            "density": "compact",
                        },
                        "text": "空间信息获取失败，请检查手机号/密码",
                    }
                )
            disk_cards.append(
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "tonal"},
                            "content": [
                                {
                                    "component": "VCardText",
                                    "content": disk_content,
                                }
                            ],
                        }
                    ],
                }
            )
        content[0]["content"].extend(disk_cards)

        # STRM 功能卡片
        if self._strm_enabled:
            strm_status_lines = []
            monitor_count = len(
                [l for l in (self._transfer_monitor_paths or "").splitlines() if l.strip()]
            )
            sync_count = len(
                [l for l in (self._full_sync_paths or "").splitlines() if l.strip()]
            )
            strm_status_lines.append(
                {
                    "component": "div",
                    "props": {"class": "text-caption"},
                    "text": f"• 整理事件监控：{'已启用' if monitor_count else '未配置'}（{monitor_count} 条路径映射）",
                }
            )
            strm_status_lines.append(
                {
                    "component": "div",
                    "props": {"class": "text-caption"},
                    "text": f"• 全量同步：{'已配置' if sync_count else '未配置'}（{sync_count} 条路径映射）"
                    f"{'，定时 ' + self._full_sync_cron if self._full_sync_cron else ''}",
                }
            )
            strm_status_lines.append(
                {
                    "component": "div",
                    "props": {"class": "text-caption"},
                    "text": f"• MoviePilot 地址：{self._moviepilot_address or '未配置（无法生成 STRM URL）'}",
                }
            )
            strm_status_lines.append(
                {
                    "component": "div",
                    "props": {"class": "text-caption"},
                    "text": (
                        "• 下载换链："
                        + (
                            f"自定义 {len(self._download_base_urls)} 个域名"
                            if self._download_base_urls
                            else "默认双通道（api.123278.com/b + yun 通道）"
                        )
                        + f"，风控自动切换{'开启' if self._download_probe else '关闭'}"
                    ),
                }
            )
            # 后台同步状态
            sync_state = self._strm.sync_status() if self._strm else None
            if sync_state and sync_state.get("running"):
                strm_status_lines.append(
                    {
                        "component": "div",
                        "props": {"class": "text-caption font-weight-bold"},
                        "text": "⏳ 正在后台全量同步，请稍后刷新页面查看结果…",
                    }
                )
            elif sync_state and sync_state.get("last_result") is not None:
                last = sync_state["last_result"]
                last_time = sync_state.get("last_time") or ""
                errs = len(last.get("errors") or [])
                last_text = (
                    f"• 上次同步：{last_time} · 生成 {last.get('ok', 0)} 个，"
                    f"跳过 {last.get('skip', 0)} 个，失败 {last.get('fail', 0)} 个"
                )
                if errs:
                    last_text += f"（{errs} 条错误）"
                strm_status_lines.append(
                    {
                        "component": "div",
                        "props": {"class": "text-caption"},
                        "text": last_text,
                    }
                )
            content[0]["content"].append(
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "tonal"},
                            "content": [
                                {
                                    "component": "VCardText",
                                    "content": [
                                        {
                                            "component": "div",
                                            "content": [
                                                {
                                                    "component": "span",
                                                    "props": {
                                                        "class": "text-subtitle-1 font-weight-bold"
                                                    },
                                                    "text": "🎬 STRM 功能",
                                                },
                                                {
                                                    "component": "VBtn",
                                                    "props": {
                                                        "color": "primary",
                                                        "variant": "tonal",
                                                        "size": "small",
                                                        "class": "ml-2",
                                                        "prepend-icon": "mdi-lightning-bolt",
                                                    },
                                                    "text": "立即全量同步",
                                                    "events": {
                                                        "click": {
                                                            "api": "plugin/P123DiskMulti/strm_sync",
                                                            "method": "post",
                                                        },
                                                    },
                                                },
                                            ],
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "mt-2"
                                            },
                                            "content": strm_status_lines,
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )

        # 分享增量同步卡片
        if self._shares:
            for task in self._shares:
                st = task.status()
                share_lines = [
                    {
                        "component": "div",
                        "props": {"class": "text-caption"},
                        "text": f"• 分享标识：{st['share_key']}（脱敏）｜目标目录：{st['target_vpath']}",
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-caption"},
                        "text": f"• 已转存：{st['synced']} 个文件",
                    },
                ]
                if st.get("note"):
                    share_lines.insert(
                        0,
                        {
                            "component": "div",
                            "props": {
                                "class": "text-subtitle-2 font-weight-bold"
                            },
                            "text": f"📝 {st['note']}",
                        },
                    )
                if st.get("running"):
                    share_lines.append(
                        {
                            "component": "div",
                            "props": {"class": "text-caption font-weight-bold"},
                            "text": "⏳ 正在后台增量转存，请稍后刷新页面查看结果…",
                        }
                    )
                elif st.get("last_result") is not None:
                    last = st["last_result"]
                    errs = len(last.get("errors") or [])
                    ck = last.get("check") or {}
                    if ck.get("success"):
                        ck_text = (
                            f"可访问（{ck.get('files', 0)} 文件 / {ck.get('dirs', 0)} 目录，"
                            f"{StringUtils.str_filesize(ck.get('total_size', 0))}）"
                        )
                    else:
                        ck_text = f"失败：{ck.get('message') or '分享不可访问'}"
                    last_text = (
                        f"• 上次检测转存：{st.get('last_time') or ''} · {ck_text}\n"
                        f"  扫描 {last.get('scanned', 0)}，新增 {last.get('copied', 0)}，"
                        f"跳过 {last.get('skipped', 0)}，失败 {last.get('failed', 0)}"
                    )
                    if errs:
                        last_text += f"（{errs} 条错误）"
                    share_lines.append(
                        {
                            "component": "div",
                            "props": {"class": "text-caption"},
                            "text": last_text,
                        }
                    )
                content[0]["content"].append(
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "content": [
                                            {
                                                "component": "div",
                                                "content": [
                                                    {
                                                        "component": "span",
                                                        "props": {
                                                            "class": "text-subtitle-1 font-weight-bold"
                                                        },
                                                        "text": f"📤 分享：{task.name}",
                                                    },
                                                    {
                                                        "component": "VBtn",
                                                        "props": {
                                                            "color": "secondary",
                                                            "variant": "tonal",
                                                            "size": "small",
                                                            "class": "ml-2",
                                                            "prepend-icon": "mdi-magnify",
                                                        },
                                                        "text": "检查内容",
                                                        "events": {
                                                            "click": {
                                                                "api": "plugin/P123DiskMulti/share/check",
                                                                "method": "get",
                                                                "params": {"name": task.name},
                                                            },
                                                        },
                                                    },
                                                    {
                                                        "component": "VBtn",
                                                        "props": {
                                                            "color": "primary",
                                                            "variant": "tonal",
                                                            "size": "small",
                                                            "class": "ml-2",
                                                            "prepend-icon": "mdi-cloud-sync",
                                                        },
                                                        "text": "⚡ 立刻检测转存",
                                                        "events": {
                                                            "click": {
                                                                "api": "plugin/P123DiskMulti/share/run",
                                                                "method": "get",
                                                                "params": {"name": task.name},
                                                            },
                                                        },
                                                    },
                                                ],
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "mt-2"},
                                                "content": share_lines,
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                )

        # 定期目录整理卡片
        if self._organize:
            organize_lines = []
            path_count = len(
                [l for l in (self._organize_paths or "").splitlines() if l.strip()]
            )
            organize_lines.append(
                {
                    "component": "div",
                    "props": {"class": "text-caption"},
                    "text": f"• 整理目录：{'已配置 ' + str(path_count) + ' 个' if path_count else '未配置'}"
                    f"{'，定时 ' + self._organize_cron if self._organize_cron else ''}",
                }
            )
            org_state = self._organize.organize_status()
            if org_state.get("running"):
                organize_lines.append(
                    {
                        "component": "div",
                        "props": {"class": "text-caption font-weight-bold"},
                        "text": "⏳ 正在后台整理，请稍后刷新页面查看结果…",
                    }
                )
            elif org_state.get("last_result") is not None:
                last = org_state["last_result"]
                last_time = org_state.get("last_time") or ""
                errs = len(last.get("errors") or [])
                last_text = (
                    f"• 上次整理：{last_time} · 已提交 {last.get('ok', 0)} 个，"
                    f"失败 {last.get('fail', 0)} 个"
                )
                if errs:
                    last_text += f"（{errs} 条错误）"
                organize_lines.append(
                    {
                        "component": "div",
                        "props": {"class": "text-caption"},
                        "text": last_text,
                    }
                )
            if org_state.get("deleted"):
                organize_lines.append(
                    {
                        "component": "div",
                        "props": {"class": "text-caption"},
                        "text": f"• 已自动清理低版本文件：{org_state.get('deleted')} 个（因媒体库已有更好文件）",
                    }
                )
            content[0]["content"].append(
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {
                            "component": "VCard",
                            "props": {"variant": "tonal"},
                            "content": [
                                {
                                    "component": "VCardText",
                                    "content": [
                                        {
                                            "component": "div",
                                            "content": [
                                                {
                                                    "component": "span",
                                                    "props": {
                                                        "class": "text-subtitle-1 font-weight-bold"
                                                    },
                                                    "text": "🗂️ 定期目录整理",
                                                },
                                                {
                                                    "component": "VBtn",
                                                    "props": {
                                                        "color": "primary",
                                                        "variant": "tonal",
                                                        "size": "small",
                                                        "class": "ml-2",
                                                        "prepend-icon": "mdi-folder-cog",
                                                    },
                                                    "text": "立即整理",
                                                    "events": {
                                                        "click": {
                                                            "api": "plugin/P123DiskMulti/organize",
                                                            "method": "post",
                                                        },
                                                    },
                                                },
                                            ],
                                        },
                                        {
                                            "component": "div",
                                            "props": {"class": "mt-2"},
                                            "content": organize_lines,
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )
        return content

    def get_module(self) -> Dict[str, Any]:
        """
        获取插件模块声明，用于胁持系统模块实现（方法名：方法实现）
        """
        return {
            "list_files": self.list_files,
            "any_files": self.any_files,
            "download_file": self.download_file,
            "upload_file": self.upload_file,
            "delete_file": self.delete_file,
            "rename_file": self.rename_file,
            "get_file_item": self.get_file_item,
            "get_parent_item": self.get_parent_item,
            "get_folder": self.get_folder,
            "snapshot_storage": self.snapshot_storage,
            "storage_usage": self.storage_usage,
            "support_transtype": self.support_transtype,
            "create_folder": self.create_folder,
            "exists": self.exists,
            "get_item": self.get_item,
        }

    @eventmanager.register(ChainEventType.StorageOperSelection)
    def storage_oper_selection(self, event: Event):
        """
        监听存储选择事件，返回当前类为操作对象
        """
        if not self._enabled or not self._api:
            return
        event_data: StorageOperSelectionEventData = event.event_data
        if event_data.storage == self._disk_name:
            # 处理云盘的操作
            event_data.storage_oper = self._api  # noqa

    @eventmanager.register(EventType.TransferComplete)
    def strm_transfer_complete(self, event: Event):
        """
        监听整理完成事件，为入库文件生成 STRM（多盘支持）
        """
        if not self._enabled or not self._strm:
            return
        if not self._transfer_monitor_paths:
            return
        item = event.event_data
        if not item:
            return
        try:
            transferinfo = item.get("transferinfo")
            target_item = getattr(transferinfo, "target_item", None)
            if not target_item or target_item.storage != self._disk_name:
                return
            target_diritem = getattr(transferinfo, "target_diritem", None)
            target_diritem_path = (
                target_diritem.path if target_diritem else ""
            )
            strm_path = self._strm.handle_transfer_complete(
                target_item=target_item,
                target_diritem_path=target_diritem_path,
                paths_text=self._transfer_monitor_paths,
            )
            if strm_path and self._refresh_mediaserver:
                self._refresh_emby([str(strm_path)])
        except Exception as e:
            logger.error(f"【123多盘】整理完成 STRM 生成失败: {e}")

    # ==================== 模块方法 ====================

    def list_files(
        self, fileitem: schemas.FileItem, recursion: bool = False
    ) -> Optional[List[schemas.FileItem]]:
        """
        查询当前目录下所有目录和文件
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None

        def __get_files(_item: FileItem, _r: Optional[bool] = False):
            """
            递归处理
            """
            _items = self._api.list(_item)
            if _items:
                if _r:
                    for t in _items:
                        if t.type == "dir":
                            __get_files(t, _r)
                        else:
                            result.append(t)
                else:
                    result.extend(_items)

        # 返回结果
        result = []
        __get_files(fileitem, recursion)
        return result

    def any_files(
        self, fileitem: schemas.FileItem, extensions: list = None
    ) -> Optional[bool]:
        """
        查询当前目录下是否存在指定扩展名任意文件
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None
        return self._api.any_files(fileitem, extensions)

    def create_folder(
        self, fileitem: schemas.FileItem, name: str
    ) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None
        return self._api.create_folder(fileitem=fileitem, name=name)

    def get_folder(self, storage: str, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录，如目录不存在则创建
        """
        if not self._api or storage != self._disk_name:
            return None
        return self._api.get_folder(path)

    def download_file(
        self, fileitem: schemas.FileItem, path: Path = None
    ) -> Optional[Path]:
        """
        下载文件
        :param fileitem: 文件项
        :param path: 本地保存路径
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None
        return self._api.download(fileitem, path)

    def upload_file(
        self, fileitem: schemas.FileItem, path: Path, new_name: Optional[str] = None
    ) -> Optional[schemas.FileItem]:
        """
        上传文件
        :param fileitem: 保存目录项
        :param path: 本地文件路径
        :param new_name: 新文件名
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None
        return self._api.upload(fileitem, path, new_name)

    def delete_file(self, fileitem: schemas.FileItem) -> Optional[bool]:
        """
        删除文件或目录
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None
        return self._api.delete(fileitem)

    def rename_file(self, fileitem: schemas.FileItem, name: str) -> Optional[bool]:
        """
        重命名文件或目录
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None
        return self._api.rename(fileitem, name)

    def exists(self, fileitem: schemas.FileItem) -> Optional[bool]:
        """
        判断文件或目录是否存在
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None
        return self._api.exists(fileitem)

    def get_item(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        查询目录或文件
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None
        return self._api.get_item(Path(fileitem.path))

    def get_file_item(self, storage: str, path: Path) -> Optional[schemas.FileItem]:
        """
        根据路径获取文件项
        """
        if not self._api or storage != self._disk_name:
            return None
        return self._api.get_item(path)

    def get_parent_item(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取上级目录项
        """
        if not self._api or fileitem.storage != self._disk_name:
            return None
        return self._api.get_parent(fileitem)

    def snapshot_storage(
        self,
        storage: str,
        path: Path,
        last_snapshot_time: float = None,
        max_depth: int = 5,
    ) -> Optional[Dict[str, Dict]]:
        """
        快照存储
        :param storage: 存储类型
        :param path: 路径
        :param last_snapshot_time: 上次快照时间，用于增量快照
        :param max_depth: 最大递归深度，避免过深遍历
        """
        if not self._api or storage != self._disk_name:
            return None
        return self._api.snapshot(
            path, last_snapshot_time=last_snapshot_time, max_depth=max_depth
        )

    def storage_usage(self, storage: str) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况（所有网盘合并）
        """
        if not self._api or storage != self._disk_name:
            return None
        return self._api.usage()

    def support_transtype(self, storage: str) -> Optional[dict]:
        """
        获取支持的整理方式
        """
        if not self._api or storage != self._disk_name:
            return None
        return self._api.support_transtype()

    # ==================== 插件 API ====================

    def api_usage(self) -> Dict[str, Any]:
        """
        获取所有网盘空间使用情况
        """
        if not self._api:
            return {"success": False, "message": "插件未启用或未配置网盘"}
        try:
            details = self._api.usage_details(force=True)
            return {"success": True, **details}
        except Exception as e:
            return {"success": False, "message": f"获取空间信息失败: {e}"}

    def api_test(self) -> Dict[str, Any]:
        """
        测试所有网盘连接
        """
        if not self._api:
            return {"success": False, "message": "插件未启用或未配置网盘"}
        try:
            results = self._api.test()
            return {
                "success": all(r.get("ok") for r in results),
                "results": results,
            }
        except Exception as e:
            return {"success": False, "message": f"测试失败: {e}"}

    def api_balance(self) -> Dict[str, Any]:
        """
        一键均衡各网盘空间
        """
        if not self._api:
            return {"success": False, "message": "插件未启用或未配置网盘"}
        try:
            result = self._api.balance()
            return {"success": True, **result}
        except Exception as e:
            return {"success": False, "message": f"均衡失败: {e}"}

    def api_redirect_url(self, request: Request):
        """
        123云盘302跳转（STRM 播放端点）

        GET /redirect_url?name=&size=&md5=&s3_key_flag=&disk=
        """
        if not self._strm:
            return JSONResponse(
                {"state": False, "message": "STRM 功能未启用"}, status_code=500
            )
        name = request.query_params.get("name", "")
        md5 = request.query_params.get("md5", "")
        disk = request.query_params.get("disk", "")
        try:
            size = int(request.query_params.get("size") or 0)
        except ValueError:
            size = 0
        s3_key_flag = request.query_params.get("s3_key_flag", "")
        user_agent = request.headers.get("User-Agent") or ""
        url = self._strm.resolve_download_url(
            name=name,
            size=size,
            md5=md5,
            s3_key_flag=s3_key_flag,
            user_agent=user_agent,
            disk_name=disk,
        )
        if not url:
            return JSONResponse(
                {"state": False, "message": "获取下载地址失败"}, status_code=500
            )
        return RedirectResponse(url, 302)

    def api_strm_sync(self) -> Dict[str, Any]:
        """
        后台全量同步生成 STRM（立即返回，同步在后台线程执行）
        """
        if not self._strm:
            return {"success": False, "message": "STRM 功能未启用"}
        try:
            started = self._strm.start_full_sync(
                self._full_sync_paths or "",
                overwrite=self._full_sync_overwrite,
                on_done=self._strm_sync_done,
            )
        except Exception as e:
            return {"success": False, "message": f"启动后台同步失败: {e}"}
        if not started:
            return {
                "success": False,
                "message": "已有全量同步任务正在进行，请稍后再试",
            }
        return {
            "success": True,
            "background": True,
            "message": "已开始后台全量同步，请稍后刷新页面查看结果",
        }

    def api_strm_status(self) -> Dict[str, Any]:
        """
        查询后台 STRM 同步状态
        """
        if not self._strm:
            return {"success": False, "message": "STRM 功能未启用"}
        try:
            return {"success": True, **self._strm.sync_status()}
        except Exception as e:
            return {"success": False, "message": f"查询同步状态失败: {e}"}

    def api_organize(self) -> Dict[str, Any]:
        """
        定期目录整理（后台，立即返回）
        """
        if not self._organize:
            return {"success": False, "message": "定期目录整理未启用（请先在设置中启用并保存）"}
        try:
            started = self._organize.start_organize(
                self._organize_paths or ""
            )
        except Exception as e:
            return {"success": False, "message": f"启动后台整理失败: {e}"}
        if not started:
            return {
                "success": False,
                "message": "已有整理任务正在进行，请稍后再试",
            }
        return {
            "success": True,
            "background": True,
            "message": "已开始后台整理：扫描目录并提交到 MoviePilot 整理队列，请稍后刷新页面查看结果",
        }

    def api_organize_status(self) -> Dict[str, Any]:
        """
        查询后台目录整理状态
        """
        if not self._organize:
            return {"success": False, "message": "定期目录整理未启用"}
        try:
            return {"success": True, **self._organize.organize_status()}
        except Exception as e:
            return {"success": False, "message": f"查询整理状态失败: {e}"}

    def _strm_sync_done(self, result: Dict[str, Any]):
        """
        后台全量同步完成回调（在后台线程中执行）

        :param result: 同步统计结果
        """
        try:
            if result.get("paths") and self._refresh_mediaserver:
                self._refresh_emby(result["paths"])
        except Exception as e:
            logger.warning(f"【123多盘】同步完成后刷新媒体服务器失败: {e}")

    def api_share_check(self, request: Request) -> Dict[str, Any]:
        """
        检查指定分享内容（可访问性 + 文件统计）

        GET /share/check?name=分享名称
        """
        task = self._find_share(request.query_params.get("name") or "")
        if not task:
            return {"success": False, "message": "分享任务不存在"}
        return task.check()

    def api_share_sync(self, request: Request) -> Dict[str, Any]:
        """
        后台增量转存指定分享（立即返回，转存在后台线程执行）

        GET /share/sync?name=分享名称
        """
        task = self._find_share(request.query_params.get("name") or "")
        if not task:
            return {"success": False, "message": "分享任务不存在"}
        try:
            started = task.start_sync()
        except Exception as e:
            return {"success": False, "message": f"启动后台转存失败: {e}"}
        if not started:
            return {
                "success": False,
                "message": "已有转存任务正在进行，请稍后再试",
            }
        return {
            "success": True,
            "background": True,
            "message": "已开始后台增量转存，请稍后刷新页面查看结果",
        }

    def api_share_run(self, request: Request) -> Dict[str, Any]:
        """
        立刻检测转存指定分享（后台执行）

        GET /share/run?name=分享名称；name 为空则依次启动全部分享任务。
        一轮完成：分享内容检测（可访问性 + 文件统计）+ 增量转存新文件。
        """
        name = request.query_params.get("name") or ""
        if name:
            task = self._find_share(name)
            if not task:
                return {"success": False, "message": "分享任务不存在"}
            tasks = [task]
        else:
            tasks = list(self._shares)
        if not tasks:
            return {"success": False, "message": "未配置分享任务"}
        started = []
        for task in tasks:
            try:
                if task.start_run():
                    started.append(task.name)
            except Exception as e:
                return {"success": False, "message": f"启动失败: {e}"}
        if not started:
            return {
                "success": False,
                "message": "已有转存任务正在运行，请稍后再试",
            }
        return {
            "success": True,
            "background": True,
            "message": f"已开始立刻检测转存：{'、'.join(started)}",
        }

    def api_share_status(self, request: Request = None) -> Dict[str, Any]:
        """
        查询所有分享任务状态
        """
        return {
            "success": True,
            "shares": [task.status() for task in self._shares],
        }

    def _refresh_emby(self, paths: List[str]):
        """
        刷新媒体服务器媒体库（批量，失败仅记录日志）

        :param paths: 需要刷新的 STRM 文件/目录路径列表
        """
        if not paths:
            return
        try:
            names_conf = self._mediaserver_names
            if isinstance(names_conf, list):
                names = [
                    str(item).strip()
                    for item in names_conf
                    if str(item).strip()
                ]
            else:
                names = [
                    item.strip()
                    for item in str(names_conf or "").split(",")
                    if item.strip()
                ]
            services = MediaServerHelper().get_services(
                name_filters=names or None
            )
            if not services:
                logger.warning("【123多盘】未找到可用的媒体服务器，跳过刷新")
                return
            refresh_items = []
            for path in dict.fromkeys(str(p) for p in paths):
                try:
                    refresh_items.append(
                        RefreshMediaItem(target_path=Path(path))
                    )
                except Exception:
                    continue
            if not refresh_items:
                return
            for service in services.values():
                try:
                    instance = getattr(service, "instance", service)
                    if hasattr(instance, "refresh_library_by_items"):
                        instance.refresh_library_by_items(refresh_items)
                    elif hasattr(instance, "refresh_library"):
                        instance.refresh_library()
                except Exception as e:
                    logger.warning(f"【123多盘】刷新媒体服务器失败: {e}")
        except Exception as e:
            logger.warning(f"【123多盘】获取媒体服务器服务失败: {e}")

    def stop_service(self):
        """
        退出插件
        """
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
        for task in self._shares:
            try:
                task.close()
            except Exception:
                pass
        self._shares = []
        self._strm = None
        self._organize = None
        self._api = None
