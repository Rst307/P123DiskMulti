"""
P123DiskMulti 核心逻辑独立测试（不依赖 MoviePilot 环境）

用内存模拟的 123 网盘 API 验证：
1. 虚拟路径拆分 / 多盘合并浏览
2. 上传时空间不足自动切换网盘
3. 跨网盘移动/复制
4. 空间合并显示
5. 一键均衡
"""
import sys
import types
from typing import Optional
from datetime import datetime
from pathlib import Path

# ============ 模拟 app 依赖 ============
app = types.ModuleType("app")
schemas = types.ModuleType("app.schemas")
from pydantic import BaseModel


class FileItem(BaseModel):
    path: Optional[str] = "/"
    storage: Optional[str] = "local"
    type: Optional[str] = None
    name: Optional[str] = None
    basename: Optional[str] = None
    extension: Optional[str] = None
    size: Optional[int] = None
    modify_time: Optional[float] = None
    fileid: Optional[str] = None
    parent_fileid: Optional[str] = None
    pickcode: Optional[str] = None


class StorageUsage(BaseModel):
    total: float = 0.0
    available: float = 0.0


FileItem.model_rebuild()


schemas.FileItem = FileItem
schemas.StorageUsage = StorageUsage
app.schemas = schemas

log = types.ModuleType("app.log")


class _L:
    def info(self, *a, **k):
        print("[INFO]", *a)

    def debug(self, *a, **k):
        pass

    def warn(self, *a, **k):
        print("[WARN]", *a)

    def warning(self, *a, **k):
        print("[WARN]", *a)

    def error(self, *a, **k):
        print("[ERROR]", *a)


logger = _L()
log.logger = logger
app.log = log

core = types.ModuleType("app.core")
config = types.ModuleType("app.core.config")


class _Settings:
    TEMP_PATH = Path("/tmp")


settings = _Settings()


class _GlobalVars:
    def is_transfer_stopped(self, *a):
        return False


global_vars = _GlobalVars()
config.settings = settings
config.global_vars = global_vars
core.config = config
app.core = core

fm = types.ModuleType("app.modules")
fms = types.ModuleType("app.modules.filemanager")
fmst = types.ModuleType("app.modules.filemanager.storages")


def transfer_process(path):
    return lambda p: None


fmst.transfer_process = transfer_process
fms.storages = fmst
fm.filemanager = fms
app.modules = fm

exc = types.ModuleType("app.schemas.exception")


class StorageQueryError(Exception):
    pass


exc.StorageQueryError = StorageQueryError
schemas.exception = exc

su = types.ModuleType("app.utils")
sus = types.ModuleType("app.utils.string")


class StringUtils:
    @staticmethod
    def str_filesize(size):
        if size >= 1024 ** 3:
            return f"{size / 1024 ** 3:.2f}GB"
        return f"{size}B"


sus.StringUtils = StringUtils
su.string = sus
app.utils = su

sys.modules["app"] = app
sys.modules["app.schemas"] = schemas
sys.modules["app.log"] = log
sys.modules["app.log.logger"] = logger
sys.modules["app.core"] = core
sys.modules["app.core.config"] = config
sys.modules["app.modules"] = fm
sys.modules["app.modules.filemanager"] = fms
sys.modules["app.modules.filemanager.storages"] = fmst
sys.modules["app.schemas.exception"] = exc
sys.modules["app.utils"] = su
sys.modules["app.utils.string"] = sus

# 模拟 p123client
p123client = types.ModuleType("p123client")


def check_response(resp):
    if isinstance(resp, dict) and resp.get("code") not in (0, None):
        raise Exception(f"api error: code={resp.get('code')}")
    return resp


p123client.check_response = check_response
# 懒加载客户端，测试中不会真正实例化
p123client.P123Client = object


class P123AuthenticationError(OSError):
    """模拟 p123client 认证异常（share_ticket 导入用）"""


p123client.P123AuthenticationError = P123AuthenticationError
sys.modules["p123client"] = p123client

# 以包方式导入插件模块（p123_api 内部使用相对导入）
_pkg = types.ModuleType("P123DiskMulti")
_pkg.__path__ = [str(Path(__file__).parent.parent / "plugins.v2" / "p123diskmulti")]
sys.modules["P123DiskMulti"] = _pkg
import P123DiskMulti.p123_api as _p123_api
DiskAccount = _p123_api.DiskAccount
P123MultiApi = _p123_api.P123MultiApi

# ============ 内存模拟的 123 客户端 ===========
class FakeP123Client:
    """模拟单个123账号：内存文件树 + 空间"""

    token = "fake-token"

    def __init__(self, total, used):
        self.total = total
        self.used = used
        self.entries = {}  # file_id -> dict
        self.next_id = 1000
        self._root_id = 0
        self.last_upload = None
        self.file_contents = {}  # file_id -> bytes
        self.pending_download_file_id = None
        self.entries[0] = {
            "FileId": 0, "FileName": "", "ParentFileId": -1, "Type": 1,
            "Size": 0, "UpdateAt": "2024-01-01T00:00:00",
        }
        # 自分享（SelfShare 测试用）
        self.shares = []          # 已创建的分享列表
        self.share_create_calls = []
        self.share_cancel_calls = []
        self.share_copy_calls = []  # share_fs_copy 调用记录（跨盘分享直传用）
        self.fail_share_create = False
        self.fail_share_fs_list_once = False
        self.share_entries = None  # 分享内文件树（None 时建分享自动快照）
        # 按需分享（OnDemand 测试用）
        self.upload_request_calls = []
        self.fs_list_calls = []
        self.fs_info_calls = []
        self.fs_list_new_calls = []
        self.search_off = False  # True 时全局搜索返回空（模拟搜索未命中）
        self.search_miss_keywords = set()  # 仅指定关键词漏索引（模拟 123 搜索索引缺口）
        self.fail_search_401 = False  # True 时全局搜索抛 401 认证异常（模拟登录态失效）
        self.fs_list_sleep = 0.0  # fs_list 每次调用延时秒数（限时定位测试用）
        self.fs_list_server_limit = 0  # >0 时服务端强制短页（即使请求 limit=100）
        self.relogin_calls = 0  # 强制重登次数（换票 5112 自愈用）

    def relogin(self):
        """模拟强制重登：换发新 token（返回新 token）"""
        self.relogin_calls += 1
        self.token = f"fake-token-new{self.relogin_calls}"
        return self.token

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    @staticmethod
    def _now():
        return datetime.now().isoformat()

    def _entry(self, name, parent, ftype, size=0, etag="", s3_key_flag="s3"):
        eid = self._new_id()
        e = {
            "FileId": eid, "FileName": name,
            "ParentFileId": int(parent) if parent is not None else 0,
            "Type": 1 if ftype == "dir" else 0, "Size": size,
            "UpdateAt": self._now(), "Etag": etag, "S3KeyFlag": s3_key_flag,
        }
        self.entries[eid] = e
        return e

    # ---- 空间 ----
    def user_info(self):
        return {"code": 0, "data": {
            "SpacePermanent": self.total, "SpaceUsed": self.used,
        }}

    def _children(self, parent_id):
        return [e for e in self.entries.values() if e["ParentFileId"] == parent_id]

    # ---- 文件操作 ----
    def fs_list(self, payload):
        """模拟 file/list：与真实 123 API 一致——只按 next 游标分页，
        Page 参数被忽略（next=0 永远返回第一页）；Next 为下一页首项 FileId，
        无更多时返回 -1。fs_list_sleep>0 时每次调用延时（模拟限时定位测试用）。"""
        self.fs_list_calls.append(dict(payload))
        parent = payload.get("parentFileId", 0)
        children = self._children(parent)
        children.sort(key=lambda e: e["FileName"])
        if getattr(self, "fs_list_sleep", 0):
            import time as _t
            _t.sleep(self.fs_list_sleep)
        limit = max(1, int(payload.get("limit", 100)))
        if getattr(self, "fs_list_server_limit", 0):
            limit = min(limit, int(self.fs_list_server_limit))
        _next = int(payload.get("next", 0) or 0)
        if _next == 0:
            start = 0
        else:
            # Next 是不透明的「下一页首项」游标，必须精确匹配；不能按
            # FileId 数值大小推断位置（响应按文件名排序时两者并不单调）。
            start = next(
                (i for i, e in enumerate(children) if int(e["FileId"]) == _next),
                len(children),
            )
        chunk = children[start:start + limit]
        end = start + len(chunk)
        if end >= len(children):
            return {"code": 0, "data": {"InfoList": [dict(e) for e in chunk], "Next": "-1"}}
        return {
            "code": 0,
            "data": {
                "InfoList": [dict(e) for e in chunk],
                "Next": str(children[end]["FileId"]),
            },
        }

    def fs_list_new(self, payload, base_url="", event="homeListFile", **kwargs):
        """模拟 web 文件列表（file/list/new）：SearchData 时全局按名模糊搜索"""
        self.fs_list_new_calls.append({
            "payload": dict(payload),
            "base_url": base_url,
            "headers": kwargs.get("headers"),
        })
        if getattr(self, "fail_search_401", False):
            raise P123AuthenticationError(
                1, {"code": 401, "message": "token contains an invalid number of segments", "data": None}
            )
        search = payload.get("SearchData") or ""
        if search:
            kw = str(search).lower()
            if (
                getattr(self, "search_off", False)
                or kw in getattr(self, "search_miss_keywords", set())
            ):
                return {"code": 0, "data": {"InfoList": [], "Next": "-1"}}
            matches = [
                e for e in self.entries.values()
                if e["Type"] == 0 and kw in str(e["FileName"]).lower()
            ]
            page = max(1, int(payload.get("Page", 1)))
            limit = max(1, int(payload.get("limit", 100)))
            chunk = matches[(page - 1) * limit: page * limit]
            return {
                "code": 0,
                "data": {
                    "InfoList": [dict(e) for e in chunk],
                    "Next": "-1",
                    "Total": len(matches),
                },
            }
        parent = int(payload.get("parentFileId", 0))
        children = self._children(parent)
        children.sort(key=lambda e: e["FileName"])
        return {"code": 0, "data": {"InfoList": children, "Next": "-1"}}

    def fs_info(self, file_id):
        self.fs_info_calls.append(file_id)
        if isinstance(file_id, dict):
            ids = file_id.get("fileIdList") or []
            file_id = (ids[0] or {}).get("FileId") if ids else 0
        e = self.entries.get(int(file_id or 0))
        if not e:
            return {"code": 1, "message": "not found"}
        return {"code": 0, "data": {"infoList": [e]}}

    def fs_mkdir(self, name, parent_id=0):
        e = self._entry(name, int(parent_id), "dir")
        return {"code": 0, "data": {"Info": e}}

    def fs_rename(self, payload):
        e = self.entries.get(int(payload["FileId"]))
        if not e:
            return {"code": 1}
        e["FileName"] = payload["fileName"]
        return {"code": 0, "data": {}}

    def fs_trash(self, file_id, event=""):
        if int(file_id) in self.entries:
            del self.entries[int(file_id)]
        return {"code": 0, "data": {}}

    def fs_move(self, file_id, parent_id=0):
        e = self.entries.get(int(file_id))
        if not e:
            return {"code": 1}
        e["ParentFileId"] = parent_id
        return {"code": 0, "data": {}}

    def fs_copy(self, file_id, parent_id=0):
        e = self.entries.get(int(file_id))
        if not e:
            return {"code": 1}
        e2 = dict(e)
        e2["FileId"] = self._new_id()
        e2["ParentFileId"] = parent_id
        e2["FileName"] = e["FileName"] + " (副本)"
        self.entries[e2["FileId"]] = e2
        return {"code": 0, "data": {}}

    # ---- 上传 ----
    def upload_request(self, payload, base_url="", **kwargs):
        self.upload_request_calls.append({"payload": dict(payload), "base_url": base_url})
        etag = payload.get("etag")
        # 秒传：文件已存在
        for e in self.entries.values():
            if e["Type"] == 0 and e.get("Etag") == etag:
                return {"code": 0, "data": {"Reuse": True, "Info": e}}
        # 占用空间
        e = self._entry(payload["fileName"], payload["parentFileId"], "file",
                        size=payload["size"], etag=etag)
        self.used += payload["size"]
        self.last_upload = e
        return {"code": 0, "data": {
            "Reuse": False, "SliceSize": 10 * 1024 * 1024, "UploadId": "u1",
            "Key": "k", "AccessKeyId": "a", "AccessKeySecret": "s", "SecurityToken": "t",
            "Bucket": "b", "EndPoint": "e",
        }}

    def upload_auth(self, data):
        return {"code": 0, "data": {"presignedUrls": {"1": "http://upload/1"}}}

    def upload_prepare(self, data):
        return {"code": 0, "data": {"presignedUrls": {"1": "http://upload/1"}}}

    def request(self, url, data=None, **kwargs):
        if self.last_upload is not None and data is not None:
            self.file_contents[self.last_upload["FileId"]] = bytes(data)
        return {"code": 0}

    def upload_file_fast(self, file_md5="", file_name="", file_size=-1,
                         parent_id=0, duplicate=0, **kwargs):
        """秒传转存：按 md5 创建条目并返回 S3KeyFlag"""
        e = self._entry(file_name, int(parent_id), "file",
                        size=max(file_size, 0), etag=file_md5 or "fast-md5")
        return {"code": 0, "data": {"Info": e}}

    def _has_dir(self, name):
        """辅助：根目录下是否存在同名目录"""
        return any(e["FileName"] == name and e["Type"] == 1
                   for e in self.entries.values() if e["ParentFileId"] == 0)

    # ---------- 分享 API（ShareSync 测试用） ----------

    def share_fs_list(self, payload, base_url="", **kwargs):
        """模拟分享目录列表：从 share_entries 按 parentFileId 取，支持分页"""
        if getattr(self, "fail_share_fs_list_once", False):
            # 模拟分享已失效（首次调用失败一次，用于重建测试）
            self.fail_share_fs_list_once = False
            raise RuntimeError("share not found or expired")
        entries = getattr(self, "share_entries", None) or {}
        if not isinstance(entries, dict):
            return {"code": 0, "data": {"InfoList": [], "Next": "-1"}}
        parent = payload.get("parentFileId", 0)
        items = entries.get(int(parent), [])
        page = max(1, int(payload.get("Page", 1)))
        limit = max(1, int(payload.get("limit", 100)))
        chunk = items[(page - 1) * limit: page * limit]
        return {"code": 0, "data": {"InfoList": [dict(e) for e in chunk], "Next": "-1"}}

    def share_fs_copy(self, payload, parent_id=0, base_url="", **kwargs):
        """模拟分享转存（服务器端直传）：把文件复制到目标盘"""
        self.share_copy_calls.append({
            "payload": dict(payload),
            "parent_id": parent_id,
            "base_url": base_url,
        })
        if getattr(self, "fail_share_copy", False):
            raise RuntimeError("fake share copy failed")
        if getattr(self, "defer_share_copy", False):
            # 模拟异步转存尚未完成：不创建条目
            return {"code": 0}
        for item in payload.get("file_list", []) or []:
            self._entry(item["file_name"], int(parent_id or 0), "file",
                        size=item.get("size") or 0, etag=item.get("etag") or "")
        return {"code": 0}

    def _snapshot_share(self, file_id_list):
        """
        建分享时把网盘内文件/目录子树快照进 share_entries（分享内独立 id 空间）

        与真实 123 一致：分享内 FileId/ParentFileId 与源盘不同，
        转存时 file_list 的 parent_file_id 必须用分享内 id。
        """
        entries = {}
        id_map = {}
        next_id = [9001]

        def snap(e):
            eid = int(e["FileId"])
            if eid in id_map:
                return id_map[eid]
            sid = next_id[0]
            next_id[0] += 1
            id_map[eid] = sid
            entry = dict(e)
            entry["FileId"] = sid
            entry["ParentFileId"] = id_map.get(
                int(entry.get("ParentFileId") or 0), 0
            )
            entries.setdefault(entry["ParentFileId"], []).append(entry)
            if entry["Type"] == 1:
                for child in self._children(eid):
                    snap(child)
            return sid

        for raw in str(file_id_list or "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            e = self.entries.get(int(raw))
            if e:
                snap(e)
        return entries

    # ---------- 自分享 API（SelfShare 测试用） ----------

    def share_create(self, payload, base_url="", **kwargs):
        """模拟建分享（web 通道）：记录调用并生成分享；
        每次建分享都重新快照网盘内容（与真实 123 一致，多盘互传用）"""
        self.share_create_calls.append({"payload": dict(payload), "base_url": base_url})
        if getattr(self, "fail_share_create", False):
            return {"code": 1, "message": "share create failed"}
        self.share_entries = self._snapshot_share(payload.get("fileIdList", ""))
        eid = self._new_id()
        share = {
            "ShareId": eid,
            "ShareKey": f"key-{eid}",
            "ShareName": payload.get("shareName", ""),
            "SharePwd": payload.get("sharePwd", ""),
            "Status": 1,
            "Expiration": payload.get("expiration", ""),
        }
        self.shares.append(share)
        return {
            "code": 0,
            "data": {
                "ShareId": eid,
                "ShareKey": share["ShareKey"],
                "SharePwd": share["SharePwd"],
            },
        }

    def user_device_list(self, payload=None, base_url="", **kwargs):
        """模拟设备列表（user/device_list）：默认一台设备，测试可改 self.devices"""
        devices = getattr(self, "devices", None)
        if devices is None:
            devices = [
                {
                    "DeviceName": "Edge浏览器",
                    "DeviceId": "dev-1",
                    "LoginTime": "2026-07-01 10:00:00",
                    "Status": "0",
                    "Platform": "web",
                }
            ]
        if getattr(self, "device_list_shape", "") == "DeviceS":
            # 真实 123 响应：data.DeviceS + 蛇形字段
            return {"code": 0, "data": {"DeviceS": [dict(d) for d in devices]}}
        return {"code": 0, "data": {"list": [dict(d) for d in devices]}}

    def share_list(self, payload, base_url="", **kwargs):
        """模拟分享列表（share/list）：分页返回，最新优先"""
        page = max(1, int(payload.get("Page", 1)))
        limit = max(1, int(payload.get("limit", 100)))
        items = [dict(s) for s in getattr(self, "shares", [])]
        items.sort(key=lambda s: s["ShareId"], reverse=True)
        chunk = items[(page - 1) * limit: page * limit]
        return {"code": 0, "data": {"InfoList": chunk, "Next": "-1"}}

    def share_cancel(self, payload, base_url="", **kwargs):
        """模拟取消分享（share/delete）"""
        self.share_cancel_calls.append({"payload": payload, "base_url": base_url})
        ids = []
        if isinstance(payload, dict):
            ids = [
                str(x.get("shareId", ""))
                for x in payload.get("shareInfoList", [])
            ]
        else:
            ids = [str(payload)]
        self.shares = [
            s for s in self.shares if str(s["ShareId"]) not in ids
        ]
        return {"code": 0}

    def upload_complete(self, data):
        # 返回本次上传创建的文件条目
        e = self.last_upload
        if e is None:
            e = self._entry(data.get("fileName", "f"), data.get("parentFileId", 0), "file")
        return {"code": 0, "data": {"file_info": e}}

    def download_info(self, payload, base_url="", async_=False, **kwargs):
        self.pending_download_file_id = int(payload.get("FileID", 0) or 0)
        self.last_download_bases = getattr(self, "last_download_bases", [])
        self.last_download_bases.append(base_url)
        return {"code": 0, "data": {"DownloadUrl": "http://download/1"}}


# 模拟 requests.get（下载）
class FakeResp:
    def __init__(self, data, status_code=206, json_data=None, headers=None):
        self._data = data
        self.status_code = status_code
        self.text = str(data)[:200]
        self._json = json_data
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def json(self):
        if self._json is not None:
            return self._json
        raise ValueError("not json")

    def close(self):
        pass

    def iter_content(self, chunk_size):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]


import tempfile
settings.TEMP_PATH = Path(tempfile.gettempdir())

_fake_clients = []


def fake_get(url, stream=True, **kwargs):
    if str(url).startswith("http://share-cdn"):
        # 分享票 210 解析：网关返回重定向 JSON（可切换为风控 403）
        if getattr(fake_get, "_share_210_403", False):
            return FakeResp(
                b"", status_code=403,
                json_data={"message": "download err: 50002", "code": 1010},
            )
        return FakeResp(
            b"", status_code=210,
            json_data={"code": 0, "data": {"redirect_url": "http://edge.example/file"}},
        )
    headers = kwargs.get("headers") or {}
    if headers.get("Range"):
        # 下载票探测请求（Range 首字节）：不消费下载内容状态，直接返回 206
        return FakeResp(b"", status_code=206)
    for fake in _fake_clients:
        if fake.pending_download_file_id is not None:
            data = fake.file_contents.get(fake.pending_download_file_id, b"")
            fake.pending_download_file_id = None
            return FakeResp(data)
    return FakeResp(b"")


# 分享票换票（share/download/info）桩：记录调用，可配置响应（dict=固定，list=按次弹出）
_share_download_calls = []


def fake_post(url, **kwargs):
    _share_download_calls.append(
        {
            "url": url,
            "json": kwargs.get("json") or {},
            "headers": kwargs.get("headers") or {},
        }
    )
    response = getattr(fake_post, "_response", None)
    if isinstance(response, list):
        response = response.pop(0) if response else None
    if response is None:
        import base64 as _b64
        inner = (
            "http://share-cdn.example/ticket?v=5"
            "&t=9999999999&r=abc&bzs=00&ur=vpngvaegvngvnp"
        )
        params = _b64.b64encode(inner.encode("utf-8")).decode("utf-8")
        response = {
            "code": 0,
            "data": {
                "DownloadURL": f"https://web-pro2.123952.com/download-v2/?params={params}"
            },
        }
    return FakeResp(response, status_code=200, json_data=response)


# 让 DiskAccount 使用我们的假客户端
# 下载换链域名逻辑与生产一致：复用 tool.py 的 exchange_and_validate
import P123DiskMulti.tool as _tool  # noqa: E402

# 回归保护：tool 模块必须持有从 p123client 导入的 P123Client
# （生产环境 P123AutoClient 懒加载客户端依赖它，丢失会 NameError 全盘失效）
assert _tool.P123Client is object, "tool.P123Client 未从 p123client 导入"


class FakeAutoClient:
    def __init__(self, fake):
        self._fake = fake
        self._dl_state = _tool.DownloadTicketState(None)

    def __getattr__(self, name):
        return getattr(self._fake, name)

    def set_download_base_urls(self, base_urls=None):
        self._dl_state.base_urls = tuple(
            base_urls or _tool.DEFAULT_DOWNLOAD_BASE_URLS
        )

    def get_download_url(self, payload, headers=None, base_urls=None,
                         probe=True, timeout=8, cache_ttl=600):
        if base_urls is not None:
            self.set_download_base_urls(base_urls)
        return _tool.exchange_and_validate(
            self._fake, payload, headers=headers, state=self._dl_state,
            probe=_tool.probe_download_url if probe else False,
            timeout=timeout, cache_ttl=cache_ttl,
        )


def make_account(name, total, used, client=None):
    fake = client or FakeP123Client(total, used)
    acc = DiskAccount(name, "13800000000", "pw")
    acc.client = FakeAutoClient(fake)
    _fake_clients.append(fake)
    return acc


# ============ 测试 ============
passed = 0
failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {msg}")
    else:
        failed += 1
        print(f"  ❌ {msg}")


_p123_api.requests.get = fake_get
_p123_api.requests.post = fake_post


def _run_tests():
    print("== 1. 路径拆分 ==")
    api = P123MultiApi(disks=[], disk_name="123云盘")
    acc_a = make_account("盘A", 100 * 1024 ** 3, 10 * 1024 ** 3)
    acc_b = make_account("盘B", 200 * 1024 ** 3, 50 * 1024 ** 3)
    api._accounts = [acc_a, acc_b]

    a, r = api._split("/")
    check(a is None and r == "/", f"_split('/') -> (None, '/')")

    a, r = api._split("/盘A/电影/x.mkv")
    check(a is acc_a and r == "/电影/x.mkv", f"_split('/盘A/电影/x.mkv') -> (盘A, '/电影/x.mkv')")

    a, r = api._split("/盘B")
    check(a is acc_b and r == "/", f"_split('/盘B') -> (盘B, '/')")

    a, r = api._split("/盘A/电影/")
    check(a is acc_a and r == "/电影", f"_split('/盘A/电影/') -> (盘A, '/电影')")

    a, r = api._split("/电影")
    check(a is None and r == "/电影", f"_split('/电影') -> (None, '/电影')")

    print("== 2. 虚拟根目录浏览 ==")
    root = FileItem(storage="123云盘", path="/", type="dir")
    items = api.list(root)
    check(len(items) == 2, "根目录列出 2 个网盘")
    check(items[0].path == "/盘A/" and items[0].type == "dir", "网盘项路径/类型正确")

    print("== 3. 网盘内浏览（路径带盘前缀） ==")
    acc_a.client._fake._entry("电影", 0, "dir")
    acc_a.client._fake._entry("电影/复仇者联盟.mkv".split("/")[1], 0, "dir")
    # 创建 电影/xxx.mkv
    mv_dir = [e for e in acc_a.client._fake.entries.values() if e["FileName"] == "电影"][0]
    acc_a.client._fake._entry("复仇者联盟.mkv", mv_dir["FileId"], "file", size=1024)

    movie_dir_item = api.get_folder(Path("/盘A/电影"))
    check(movie_dir_item is not None, "get_folder('/盘A/电影') 成功")
    check(movie_dir_item.path == "/盘A/电影/", f"路径正确: {movie_dir_item.path}")

    files = api.list(movie_dir_item)
    check(len(files) == 1 and files[0].name == "复仇者联盟.mkv", "列出电影目录文件")
    check(files[0].path == "/盘A/电影/复仇者联盟.mkv", f"文件路径带盘前缀: {files[0].path}")

    print("== 4. 空间合并 ==")
    usage = api.usage()
    check(usage.total == 300 * 1024 ** 3, f"合并总空间: {usage.total}")
    check(usage.available == 240 * 1024 ** 3, f"合并剩余空间: {usage.available}")

    print("== 5. 上传空间不足自动切换 ==")
    # 盘A 只剩 0.5MB，盘B 剩余很多；文件 2MB → 应自动切到盘B
    acc_a.client._fake.total = 100 * 1024 * 1024  # 盘A total 100MB
    acc_a.client._fake.used = 99.5 * 1024 * 1024  # 剩余 0.5MB
    api._reserve_size = 0
    api._auto_switch = True
    api._invalidate_usage()
    tmp = Path("/tmp/test_movie.mkv")
    tmp.write_bytes(b"x" * (2 * 1024 * 1024))  # 2MB 文件

    # 清掉盘A上已有的同名/同内容文件，避免秒传干扰
    for eid in [eid for eid, e in acc_a.client._fake.entries.items() if e["FileName"] == "新电影.mkv"]:
        del acc_a.client._fake.entries[eid]
    acc_a.client._fake.used = 99.5 * 1024 * 1024

    fake_b = acc_b.client._fake
    for eid in [eid for eid, e in fake_b.entries.items() if e["FileName"] == "新电影.mkv"]:
        del fake_b.entries[eid]
    fake_b.used = 50 * 1024 ** 3
    api._invalidate_usage()

    # 让盘A目录结构重建（旧文件被删除后目录仍存在）
    dir_a = api.get_folder(Path("/盘A/电影"))
    check(dir_a is not None, "获取盘A目标目录")
    new_item = api.upload(dir_a, tmp, "新电影.mkv")
    check(new_item is not None, "上传成功")
    check(str(new_item.path).startswith("/盘B/"), f"自动切换到盘B: {new_item.path}")
    check("新电影.mkv" in [e["FileName"] for e in fake_b.entries.values()], "文件实际在盘B")

    print("== 6. 关闭自动切换时上传到指定盘 ==")
    api._auto_switch = False
    dir_a = api.get_folder(Path("/盘A/电影"))
    new_item2 = api.upload(dir_a, tmp, "电影2.mkv")
    check(str(new_item2.path).startswith("/盘A/"), f"仍在盘A: {new_item2.path}")
    api._auto_switch = True

    print("== 7. 未指定网盘时自动分配 ==")
    new_item3 = api.upload(FileItem(storage="123云盘", path="/自动目录/", type="dir"), tmp, "电影3.mkv")
    check(new_item3 is not None and str(new_item3.path).startswith("/盘B/"), f"自动分配到剩余空间最大网盘: {new_item3.path}")

    print("== 8. 跨网盘移动（临时分享 + 服务器端直传） ==")
    # 把盘B电影目录的 新电影.mkv 移动到 盘A/电影（先恢复盘A空间，避免切盘干扰）
    acc_a.client._fake.total = 300 * 1024 ** 3
    acc_a.client._fake.used = 50 * 1024 ** 3
    api._invalidate_usage()
    src = api.get_item(Path(new_item.path))
    check(src is not None, f"获取源文件: {new_item.path}")
    acc_b.client._fake.share_create_calls.clear()
    acc_b.client._fake.share_cancel_calls.clear()
    acc_a.client._fake.share_copy_calls.clear()
    ok = api.move(src, Path("/盘A/电影/"), src.name)
    check(ok, "跨盘移动成功")
    check(
        len(acc_b.client._fake.share_create_calls) == 1,
        "源盘建临时分享（不下载本地）",
    )
    check(
        len(acc_b.client._fake.share_cancel_calls) == 1,
        "转存完成后取消临时分享",
    )
    check(
        len(acc_a.client._fake.share_copy_calls) == 1,
        "目标盘服务器端直传转存",
    )
    dst = api.get_item(Path("/盘A/电影/新电影.mkv"))
    check(dst is not None, "文件已出现在盘A目标目录")
    src2 = api.get_item(Path("/盘B/电影/新电影.mkv"))
    check(src2 is None, "盘B源文件已删除")

    print("== 9. 同网盘移动 ==")
    src3 = api.get_item(Path("/盘A/电影/复仇者联盟.mkv"))
    ok = api.move(src3, Path("/盘A/"), "复联.mkv")
    check(ok, "同盘移动成功")
    check(api.get_item(Path("/盘A/复联.mkv")) is not None, "文件在盘A根目录")
    check(api.get_item(Path("/盘A/电影/复仇者联盟.mkv")) is None, "原位置已移除")

    print("== 10. 跨网盘复制（保留源，分享直传） ==")
    src4 = api.get_item(Path("/盘A/复联.mkv"))
    acc_a.client._fake.share_create_calls.clear()
    acc_a.client._fake.share_cancel_calls.clear()
    ok = api.copy(src4, Path("/盘B/电影/"), "复联副本.mkv")
    check(ok, "跨盘复制成功")
    check(
        len(acc_a.client._fake.share_create_calls) == 1,
        "跨盘复制走临时分享直传",
    )
    check(
        len(acc_a.client._fake.share_cancel_calls) == 1,
        "复制后取消临时分享",
    )
    check(api.get_item(Path("/盘A/复联.mkv")) is not None, "源文件保留")
    check(api.get_item(Path("/盘B/电影/复联副本.mkv")) is not None, "副本在盘B")

    print("== 11. 目录跨盘移动（分享直传） ==")
    api.create_folder(FileItem(storage="123云盘", path="/盘A/", type="dir"), "剧集")
    api.create_folder(FileItem(storage="123云盘", path="/盘A/剧集/", type="dir"), "S01")
    # 往 剧集/S01 放个文件
    s01 = api.get_folder(Path("/盘A/剧集/S01"))
    tmp2 = Path("/tmp/test_ep.mkv")
    tmp2.write_bytes(b"y" * 1024)
    api.upload(s01, tmp2, "E01.mkv")
    tv = api.get_item(Path("/盘A/剧集"))
    acc_a.client._fake.share_create_calls.clear()
    acc_a.client._fake.share_cancel_calls.clear()
    acc_b.client._fake.share_copy_calls.clear()
    ok = api.move(tv, Path("/盘B/"), "剧集")
    check(ok, "目录跨盘移动成功")
    check(
        len(acc_a.client._fake.share_create_calls) == 1,
        "目录跨盘移动：源盘建一次临时分享（整目录）",
    )
    check(
        len(acc_a.client._fake.share_cancel_calls) == 1,
        "目录转存完成后取消临时分享",
    )
    check(
        len(acc_b.client._fake.share_copy_calls) >= 1,
        "目标盘批量服务器端直传",
    )
    check(api.get_item(Path("/盘B/剧集/S01/E01.mkv")) is not None, "目录内文件递归移动")
    check(api.get_item(Path("/盘A/剧集")) is None, "盘A原目录已删除")

    print("== 11.6 跨盘移动分享直传失败自动回退本地中转 ==")
    # 源盘 B 建分享失败 -> 自动回退 下载->上传，移动仍成功
    fake_b2 = acc_b.client._fake
    tmp3 = Path("/tmp/test_fallback.mkv")
    tmp3.write_bytes(b"z" * 2048)
    dir_b = api.get_folder(Path("/盘B/电影"))
    fb_item = api.upload(dir_b, tmp3, "回退测试.mkv")
    check(fb_item is not None, "上传回退测试文件")
    fake_b2.fail_share_create = True
    fake_b2.share_create_calls.clear()
    fake_b2.share_cancel_calls.clear()
    ok = api.move(fb_item, Path("/盘A/电影/"), "回退测试.mkv")
    fake_b2.fail_share_create = False
    check(ok, "建分享失败时回退本地中转，移动仍成功")
    check(len(fake_b2.share_create_calls) == 1, "尝试过一次建分享")
    check(len(fake_b2.share_cancel_calls) == 0, "分享未创建成功，无需取消")
    check(
        api.get_item(Path("/盘A/电影/回退测试.mkv")) is not None,
        "回退后文件到达目标盘",
    )
    check(
        api.get_item(Path("/盘B/电影/回退测试.mkv")) is None,
        "回退移动后源文件已删除",
    )

    print("== 11.7 跨盘移动目标盘空间不足，转存自动切换网盘 ==")
    # 盘B 建文件，盘A 空间不足（恢复到 0.5MB），新增盘C -> 转存应切到盘C
    acc_c = make_account("盘C", 300 * 1024 ** 3, 10 * 1024 ** 3)
    api._accounts.append(acc_c)
    acc_a.client._fake.total = 100 * 1024 * 1024
    acc_a.client._fake.used = 99.5 * 1024 * 1024
    api._invalidate_usage()
    tmp4 = Path("/tmp/test_switch.mkv")
    tmp4.write_bytes(b"w" * 1024 * 1024)  # 1MB > 盘A剩余0.5MB，触发切盘
    dir_b2 = api.get_folder(Path("/盘B/电影"))
    sw_item = api.upload(dir_b2, tmp4, "切换测试.mkv")
    check(sw_item is not None, "上传切换测试文件")
    acc_b.client._fake.share_create_calls.clear()
    acc_b.client._fake.share_cancel_calls.clear()
    acc_c.client._fake.share_copy_calls.clear()
    ok = api.move(sw_item, Path("/盘A/电影/"), "切换测试.mkv")
    check(ok, "目标盘空间不足时转存自动切换网盘，移动成功")
    check(
        len(acc_b.client._fake.share_create_calls) == 1,
        "仍由源盘建临时分享",
    )
    check(
        len(acc_b.client._fake.share_cancel_calls) == 1,
        "切盘转存后取消临时分享",
    )
    check(
        len(acc_c.client._fake.share_copy_calls) == 1,
        "转存自动切到剩余空间最大的盘C",
    )
    check(
        api.get_item(Path("/盘C/电影/切换测试.mkv")) is not None,
        "文件落在盘C（原目录结构保留）",
    )
    check(
        api.get_item(Path("/盘B/电影/切换测试.mkv")) is None,
        "盘B源文件已删除",
    )

    print("== 11.8 转存选盘须满足: 剩余空间-预留空间-文件大小>0 ==")
    # 盘A 可用 2MB，文件 1MB，预留 1.5MB：2MB < 1MB+1.5MB -> 必须切盘
    acc_a.client._fake.total = 100 * 1024 * 1024
    acc_a.client._fake.used = 98 * 1024 * 1024
    api._reserve_size = int(1.5 * 1024 * 1024)
    api._invalidate_usage()
    tmp5 = Path("/tmp/test_reserve.mkv")
    tmp5.write_bytes(b"v" * (1024 * 1024))
    dir_b3 = api.get_folder(Path("/盘B/电影"))
    rv_item = api.upload(dir_b3, tmp5, "预留测试.mkv")
    check(rv_item is not None, "上传预留测试文件")
    acc_b.client._fake.share_create_calls.clear()
    acc_b.client._fake.share_cancel_calls.clear()
    acc_c.client._fake.share_copy_calls.clear()
    ok = api.move(rv_item, Path("/盘A/电影/"), "预留测试.mkv")
    check(ok, "预留空间不足时转存自动切盘，移动成功")
    check(
        len(acc_b.client._fake.share_create_calls) == 1,
        "仍由源盘建临时分享",
    )
    check(
        len(acc_c.client._fake.share_copy_calls) == 1,
        "2MB可用 < 1MB文件+1.5MB预留，切到盘C",
    )
    check(
        api.get_item(Path("/盘C/电影/预留测试.mkv")) is not None,
        "文件落在盘C",
    )
    check(
        api.get_item(Path("/盘B/电影/预留测试.mkv")) is None,
        "盘B源文件已删除",
    )
    # 对照组：预留调为 0 后同一容量不切盘（证明是预留空间导致切盘）
    api._reserve_size = 0
    acc_a.client._fake.used = 98 * 1024 * 1024
    api._invalidate_usage()
    tmp6 = Path("/tmp/test_reserve2.mkv")
    tmp6.write_bytes(b"u" * (1024 * 1024))
    dir_b4 = api.get_folder(Path("/盘B/电影"))
    rv2 = api.upload(dir_b4, tmp6, "预留测试2.mkv")
    check(rv2 is not None, "上传对照组文件")
    acc_b.client._fake.share_create_calls.clear()
    acc_b.client._fake.share_cancel_calls.clear()
    acc_a.client._fake.share_copy_calls.clear()
    acc_c.client._fake.share_copy_calls.clear()
    ok = api.move(rv2, Path("/盘A/电影/"), "预留测试2.mkv")
    check(ok, "预留为0且空间足够时不切盘，移动成功")
    check(
        len(acc_a.client._fake.share_copy_calls) == 1,
        "预留0时 2MB可用>=1MB文件，直接在盘A转存",
    )
    check(
        api.get_item(Path("/盘A/电影/预留测试2.mkv")) is not None,
        "文件落在盘A",
    )
    check(
        api.get_item(Path("/盘B/电影/预留测试2.mkv")) is None,
        "盘B源文件已删除",
    )

    print("== 12. 删除 ==")
    victim = api.get_item(Path("/盘A/电影/新电影.mkv"))
    check(victim is not None, "获取待删除文件")
    check(api.delete(victim), "删除成功")
    check(api.get_item(Path("/盘A/电影/新电影.mkv")) is None, "文件已删除")

    print("== 13. 一键均衡 ==")
    acc_a.client._fake.used = 99.9 * 1024 ** 3  # 盘A几乎满
    acc_b.client._fake.used = 10 * 1024 ** 3   # 盘B还有很多
    api._reserve_size = 5 * 1024 ** 3
    api._invalidate_usage()
    # 在盘A根目录放一个旧文件
    acc_a.client._fake._entry("旧文件.bin", 0, "file", size=1024 * 1024)
    result = api.balance(max_items=5)
    check(result["count"] == 1, f"均衡移动了 {result['count']} 个文件")
    if result["moved"]:
        check(
            not result["moved"][0]["to"].startswith("/盘A/"),
            f"旧文件从盘A移到剩余空间更大的盘: {result['moved'][0]}",
        )

    print("== 14. 快照 ==")
    snap = api.snapshot(Path("/盘B/剧集"))
    check("/盘B/剧集/S01/E01.mkv" in snap, "快照包含盘B剧集文件")

    print("== 15. 设备列表 ==")
    devs = api.device_list()
    check(len(devs) == len(api._accounts), "每个账号返回设备列表")
    check(all(d["ok"] for d in devs), "设备列表获取成功")
    check(devs[0]["count"] == 1, f"账号包含设备记录: {devs[0]['count']}")
    check(
        devs[0]["devices"][0]["name"] == "Edge浏览器"
        and devs[0]["devices"][0]["id"] == "dev-1",
        "设备字段解析正确",
    )
    # 多设备 + 字段缺失兜底
    fake_dev_a = [
        {"DeviceName": "手机", "DeviceId": "dev-a", "LoginTime": "2026-06-01"},
        {"DeviceName": "电脑", "DeviceId": "dev-b"},
        {"unknown": "raw"},
    ]
    acc_a.client._fake.devices = fake_dev_a
    devs = api.device_list()
    d0 = [d for d in devs if d["name"] == "盘A"][0]
    check(d0["count"] == 3, "多设备记录解析")
    check(d0["devices"][0]["platform"] == "", "缺失字段兜底为空")
    check(d0["devices"][2]["name"] == "未知设备", "未知字段设备显示未知设备")
    # 真实 123 响应格式（data.DeviceS + 蛇形字段）：修复前解析不到任何设备
    fake_dev_b = [
        {
            "device_name": "I7UgGU1tgfkRzWgVRwDzz3S5XUrWp5mU",
            "plat_form": "unknown",
            "ip": "180.125.10.108",
            "last_login_time": "2026-08-11 13:08:27",
            "device_type": "未知设备",
            "key": "289d63d26a048fc0c8c7d8607895d611",
            "cur_device": True,
            "login_type": "账号登录",
        },
        {
            "device_name": "Chrome",
            "plat_form": "web",
            "ip": "180.125.10.108",
            "last_login_time": "2026-08-11 13:02:31",
            "device_type": "Windows网页版",
            "key": "39ba2b6a533a7bafb4cf0064b2a4a567",
            "cur_device": False,
            "login_type": "账号登录",
        },
    ]
    acc_a.client._fake.devices = fake_dev_b
    acc_a.client._fake.device_list_shape = "DeviceS"
    devs = api.device_list()
    d0 = [d for d in devs if d["name"] == "盘A"][0]
    check(d0["count"] == 2, f"DeviceS 格式解析: {d0['count']} 台设备")
    check(
        d0["devices"][0]["id"] == "289d63d26a048fc0c8c7d8607895d611"
        and d0["devices"][0]["time"] == "2026-08-11 13:08:27"
        and d0["devices"][0]["platform"] == "未知设备",
        "DeviceS 蛇形字段解析正确",
    )
    check(
        d0["devices"][1]["name"] == "Chrome"
        and d0["devices"][1]["platform"] == "Windows网页版",
        "DeviceS 真实设备字段解析",
    )
    del acc_a.client._fake.device_list_shape

    print("== 16. 强制重新登录 ==")
    acc_a.client._fake.relogin_calls = 0
    acc_b.client._fake.relogin_calls = 0
    results = api.relogin_all()
    check(all(r["ok"] for r in results), "强制重新登录全部成功")
    check(
        acc_a.client._fake.relogin_calls == 1
        and acc_b.client._fake.relogin_calls == 1,
        f"每个账号各重登一次: A={acc_a.client._fake.relogin_calls} B={acc_b.client._fake.relogin_calls}",
    )
    check(
        results[0]["message"] == "重新登录成功（新 token 已持久化）",
        "重登结果消息",
    )

    print()
    print(f"结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _run_tests()
