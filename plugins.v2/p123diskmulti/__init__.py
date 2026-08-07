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

from .p123_api import DiskAccount, P123MultiApi
from .strm import DEFAULT_MEDIA_EXTS, StrmHelper

# 默认网盘名称（旧版单盘配置迁移时使用）
LEGACY_DISK_NAME = "盘1"
# 默认全量同步 cron（每 7 小时）
DEFAULT_STRM_CRON = "0 */7 * * *"


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
    plugin_version = "1.1.0"
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

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        self._api = None
        self._strm = None
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
        定时全量同步 STRM
        """
        if not self._strm:
            return
        try:
            result = self._strm.full_sync(
                self._full_sync_paths or "",
                overwrite=self._full_sync_overwrite,
            )
            if result.get("paths") and self._refresh_mediaserver:
                self._refresh_emby(result["paths"])
        except Exception as e:
            logger.error(f"【123多盘】定时全量同步 STRM 失败: {e}")

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
                "methods": ["GET"],
                "summary": "123云盘302跳转（STRM播放）",
                "description": "根据文件标识实时换取 123 下载地址并 302 重定向，供 Emby 播放 STRM 使用",
            },
            {
                "path": "/strm_sync",
                "endpoint": self.api_strm_sync,
                "auth": "bear",
                "methods": ["POST"],
                "summary": "全量同步生成STRM",
                "description": "扫描所有网盘的媒体目录，批量生成 STRM 文件到本地",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "mediaserver_names",
                                            "label": "媒体服务器名称",
                                            "placeholder": "Emby",
                                            "hint": "逗号分隔多个，留空则刷新所有媒体服务器",
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
        全量同步生成 STRM（扫描所有网盘）
        """
        if not self._strm:
            return {"success": False, "message": "STRM 功能未启用"}
        try:
            result = self._strm.full_sync(
                self._full_sync_paths or "",
                overwrite=self._full_sync_overwrite,
            )
            if result.get("paths") and self._refresh_mediaserver:
                self._refresh_emby(result["paths"])
            result["success"] = True
            return result
        except Exception as e:
            return {"success": False, "message": f"全量同步失败: {e}"}

    def _refresh_emby(self, paths: List[str]):
        """
        刷新媒体服务器媒体库（批量，失败仅记录日志）

        :param paths: 需要刷新的 STRM 文件/目录路径列表
        """
        if not paths:
            return
        try:
            names = [
                item.strip()
                for item in str(self._mediaserver_names or "").split(",")
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
        self._strm = None
        self._api = None
