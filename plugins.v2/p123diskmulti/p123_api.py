import ast
import threading
import time
from datetime import datetime
from hashlib import md5
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from p123client import check_response

from app import schemas
from app.core.config import settings, global_vars
from app.log import logger
from app.modules.filemanager.storages import transfer_process
from app.schemas.exception import StorageQueryError
from app.utils.string import StringUtils

from .tool import P123AutoClient, TokenStore


class DiskAccount:
    """
    单个 123 网盘账号
    """

    def __init__(
        self,
        name: str,
        passport: str,
        password: str,
        token_store: Optional[TokenStore] = None,
    ):
        self.name = name
        self.passport = passport
        self.password = password
        self.client = P123AutoClient(passport, password, token_store=token_store)
        # 盘内路径 -> FileId 缓存（真实路径，不含盘前缀）
        self._id_cache: Dict[str, str] = {}
        self._cache_lock = threading.RLock()

    def cache_get(self, path: str) -> Optional[str]:
        """获取路径ID缓存"""
        with self._cache_lock:
            return self._id_cache.get(path)

    def cache_set(self, path: str, file_id: str):
        """设置路径ID缓存"""
        with self._cache_lock:
            self._id_cache[path] = file_id

    def clear_cache(self):
        """清空路径ID缓存（移动/重命名/删除后调用，保证一致性）"""
        with self._cache_lock:
            self._id_cache.clear()

    def __repr__(self):
        return f"<DiskAccount {self.name}>"


class P123MultiApi:
    """
    123 云盘多盘合并基础操作类

    将多个 123 网盘账号合并为一个虚拟存储（如「123云盘」），
    根目录下每个网盘对应一个文件夹（/盘名/...），实现：
    - 多盘共用：所有账号在一个存储内浏览、管理
    - 跨盘互传：move/copy 时自动识别跨盘并走 下载->上传->删除
    - 自动切换：上传时目标盘空间不足，自动选择剩余空间最大的网盘
    - 空间合并：usage() 返回所有网盘的空间总和
    """

    # 空间信息缓存有效期（秒）
    _USAGE_TTL = 60
    # 是否检查目录修改时间以跳过增量快照
    snapshot_check_folder_modtime = True

    def __init__(
        self,
        disks: List[DiskAccount],
        disk_name: str = "123云盘",
        reserve_size: int = 0,
        auto_switch: bool = True,
    ):
        """
        :param disks: 网盘账号列表
        :param disk_name: 存储名称
        :param reserve_size: 每个网盘预留空间（字节），剩余空间低于该值视为空间不足
        :param auto_switch: 是否启用空间不足自动切换网盘
        """
        self._disk_name = disk_name
        self._accounts: List[DiskAccount] = disks or []
        self._reserve_size = int(reserve_size or 0)
        self._auto_switch = auto_switch
        self.transtype = {"move": "移动", "copy": "复制"}
        self._usage_cache: Dict[str, Tuple[float, Tuple[int, int]]] = {}

    # ==================== 路径解析 ====================

    def _split(self, path: str) -> Tuple[Optional[DiskAccount], str]:
        """
        将虚拟路径拆分为 (网盘账号, 网盘内真实路径)

        '/盘A/电影/x.mkv' -> (盘A账号, '/电影/x.mkv')
        '/盘A'           -> (盘A账号, '/')
        '/'              -> (None, '/')
        '/电影'          -> (None, '/电影')   # 无盘前缀的虚拟路径
        """
        path = str(path or "/").replace("\\", "/")
        if path == "/":
            return None, "/"
        parts = path.strip("/").split("/", 1)
        for acc in self._accounts:
            if acc.name == parts[0]:
                real = "/" + parts[1] if len(parts) > 1 else "/"
                return acc, real
        return None, path

    def _root_item(self) -> schemas.FileItem:
        """虚拟根目录文件项"""
        return schemas.FileItem(
            storage=self._disk_name,
            fileid="0",
            path="/",
            name="",
            basename="",
            type="dir",
        )

    def _disk_root_item(self, account: DiskAccount) -> schemas.FileItem:
        """网盘根目录文件项（虚拟根目录下的文件夹）"""
        return schemas.FileItem(
            storage=self._disk_name,
            fileid="0",
            path=f"/{account.name}/",
            name=account.name,
            basename=account.name,
            type="dir",
            parent_fileid=None,
        )

    # ==================== 路径 -> ID ====================

    def _path_to_id(self, account: DiskAccount, path: str) -> str:
        """
        通过路径获取网盘内文件ID

        :param account: 网盘账号
        :param path: 网盘内真实路径
        :return: 文件ID字符串
        :raises FileNotFoundError: 当路径不存在时抛出异常
        """
        # 根目录
        if path == "/":
            return "0"
        path = str(path).replace("\\", "/")
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        # 检查缓存
        if cached := account.cache_get(path):
            return cached
        # 逐级查找缓存
        current_id = 0
        parent_path = "/"
        for p in Path(path).parents:
            p_str = str(p).replace("\\", "/")
            if cached := account.cache_get(p_str):
                parent_path = p_str
                current_id = int(cached)
                break
        # 计算相对路径
        rel_path = Path(path).relative_to(parent_path)
        for part in Path(rel_path).parts:
            find_part = False
            page = 1
            _next = 0
            first_find = True
            while True:
                payload = {
                    "limit": 100,
                    "next": _next,
                    "Page": page,
                    "parentFileId": int(current_id),
                    "inDirectSpace": "false",
                }
                if first_find:
                    first_find = False
                else:
                    time.sleep(1)
                resp = account.client.fs_list(payload)
                check_response(resp)
                item_list = resp.get("data").get("InfoList")
                if not item_list:
                    break
                for item in item_list:
                    if item["FileName"] == part:
                        current_id = item["FileId"]
                        find_part = True
                        break
                if find_part:
                    break
                if resp.get("data").get("Next") == "-1":
                    break
                else:
                    page += 1
                    _next = resp.get("data").get("Next")
            if not find_part:
                raise FileNotFoundError(f"【123多盘】{account.name}:{path} 不存在")
        if not current_id:
            raise FileNotFoundError(f"【123多盘】{account.name}:{path} 不存在")
        # 缓存路径
        account.cache_set(path, str(current_id))
        return str(current_id)

    # ==================== 空间管理 ====================

    def _invalidate_usage(self, account: Optional[DiskAccount] = None):
        """使空间缓存失效"""
        if account:
            self._usage_cache.pop(account.name, None)
        else:
            self._usage_cache.clear()

    def _usage_of(
        self, account: DiskAccount, force: bool = False
    ) -> Optional[Tuple[int, int]]:
        """
        获取单个网盘空间使用情况 (total, used)，带缓存

        :param account: 网盘账号
        :param force: 是否强制刷新缓存
        :return: (总空间, 已用空间)，失败返回 None
        """
        now = time.time()
        if not force:
            cached = self._usage_cache.get(account.name)
            if cached and now - cached[0] < self._USAGE_TTL:
                return cached[1]
        try:
            resp = account.client.user_info()
            check_response(resp)
            total = int(resp["data"]["SpacePermanent"])
            used = int(resp["data"]["SpaceUsed"])
            self._usage_cache[account.name] = (now, (total, used))
            return total, used
        except Exception as e:
            logger.warn(f"【123多盘】{account.name} 获取空间信息失败: {e}")
            return None

    def _has_space(self, account: DiskAccount, need: int) -> bool:
        """
        判断网盘剩余空间是否足够容纳 need 字节（含预留空间）
        查询失败时放行，避免误判
        """
        usage = self._usage_of(account)
        if not usage:
            return True
        available = usage[0] - usage[1]
        return available >= need + self._reserve_size

    def _pick_disk(
        self, need_size: int = 0, exclude: Optional[DiskAccount] = None
    ) -> Optional[DiskAccount]:
        """
        选择剩余空间最大的可用网盘

        :param need_size: 需要容纳的文件大小（字节）
        :param exclude: 排除的网盘
        :return: 网盘账号，无可用网盘返回 None
        """
        best, best_avail = None, -1
        for acc in self._accounts:
            if acc is exclude:
                continue
            usage = self._usage_of(acc)
            if not usage:
                continue
            available = usage[0] - usage[1]
            if available >= need_size + self._reserve_size and available > best_avail:
                best, best_avail = acc, available
        return best

    # ==================== 浏览 ====================

    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        浏览文件或目录

        :param fileitem: 文件项，可以是文件或目录
        :return: 文件项列表；虚拟根目录返回网盘列表，目录返回子项，文件返回自身
        """
        if fileitem.type == "file":
            item = self.detail(fileitem)
            if item:
                return [item]
            return []
        account, real = self._split(fileitem.path)
        if account is None:
            # 虚拟根目录：返回所有网盘
            return [self._disk_root_item(acc) for acc in self._accounts]
        return self._list_account(account, real)

    def _list_account(
        self, account: DiskAccount, real: str
    ) -> List[schemas.FileItem]:
        """
        浏览网盘内目录

        :param account: 网盘账号
        :param real: 网盘内真实路径
        :return: 文件项列表
        """
        if real == "/":
            file_id = "0"
        else:
            file_id = self._path_to_id(account, real)

        items = []
        try:
            page = 1
            _next = 0
            first_find = True
            while True:
                payload = {
                    "limit": 100,
                    "next": _next,
                    "Page": page,
                    "parentFileId": int(file_id),
                    "inDirectSpace": "false",
                }
                if first_find:
                    first_find = False
                else:
                    time.sleep(1)
                resp = account.client.fs_list(payload)
                check_response(resp)
                item_list = resp.get("data").get("InfoList")
                if not item_list:
                    break
                for item in item_list:
                    real_path = (
                        f"{real}{item['FileName']}"
                        if real.endswith("/")
                        else f"{real}/{item['FileName']}"
                    )
                    account.cache_set(real_path, str(item["FileId"]))

                    file_path = f"/{account.name}{real_path}" + (
                        "/" if item["Type"] == 1 else ""
                    )
                    items.append(
                        schemas.FileItem(
                            storage=self._disk_name,
                            fileid=str(item["FileId"]),
                            parent_fileid=str(item["ParentFileId"]),
                            name=item["FileName"],
                            basename=Path(item["FileName"]).stem,
                            extension=Path(item["FileName"]).suffix[1:]
                            if item["Type"] == 0
                            else None,
                            type="dir" if item["Type"] == 1 else "file",
                            path=file_path,
                            size=item["Size"] if item["Type"] == 0 else None,
                            modify_time=int(
                                datetime.fromisoformat(item["UpdateAt"]).timestamp()
                            ),
                            pickcode=str(item),
                        )
                    )
                if resp.get("data").get("Next") == "-1":
                    break
                else:
                    page += 1
                    _next = resp.get("data").get("Next")
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.debug(f"【123多盘】{account.name} 获取信息失败: {str(e)}")
            return items
        return items

    def any_files(
        self, fileitem: schemas.FileItem, extensions: Optional[list] = None
    ) -> bool:
        """
        查询当前目录下是否存在指定扩展名任意文件
        """
        def __any_file(_item: schemas.FileItem) -> bool:
            _items = self.list(_item)
            if _items:
                if not extensions:
                    return True
                for t in _items:
                    if (
                        t.type == "file"
                        and t.extension
                        and f".{t.extension.lower()}" in extensions
                    ):
                        return True
                    elif t.type == "dir":
                        if __any_file(t):
                            return True
            return False

        return __any_file(fileitem)

    # ==================== 目录 ====================

    def create_folder(
        self, fileitem: schemas.FileItem, name: str
    ) -> Optional[schemas.FileItem]:
        """
        创建目录

        :param fileitem: 父目录文件项
        :param name: 要创建的目录名称
        :return: 创建成功返回目录文件项，失败返回None
        """
        try:
            account, real = self._split(fileitem.path)
            if account is None:
                # 在虚拟路径下创建目录：自动选择网盘
                account = self._pick_disk()
                if not account:
                    logger.error(f"【123多盘】没有可用网盘，无法创建目录 {name}")
                    return None
                real = "/" if str(fileitem.path) == "/" else str(fileitem.path)
            new_real = f"{real}{name}" if real.endswith("/") else f"{real}/{name}"
            resp = account.client.fs_mkdir(
                name, parent_id=self._path_to_id(account, real)
            )
            check_response(resp)
            logger.debug(f"【123多盘】{account.name} 创建目录: {resp}")
            data = resp["data"]["Info"]
            # 缓存新目录
            account.cache_set(new_real, str(data["FileId"]))
            return schemas.FileItem(
                storage=self._disk_name,
                fileid=str(data["FileId"]),
                path=f"/{account.name}{new_real}/",
                name=name,
                basename=name,
                type="dir",
                modify_time=int(
                    datetime.fromisoformat(data["UpdateAt"]).timestamp()
                ),
                pickcode=str(data),
            )
        except Exception as e:
            logger.debug(f"【123多盘】创建目录失败: {str(e)}")
            return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录，如目录不存在则创建；
        路径未指定网盘时（如 /电影）自动选择剩余空间最大的网盘

        :param path: 目录路径
        :return: 目录文件项，如果创建失败则返回None
        """
        account, real = self._split(str(path))
        if account is None:
            if str(path) == "/":
                return self._root_item()
            account = self._pick_disk()
            if not account:
                logger.error(f"【123多盘】没有可用网盘，无法创建目录 {path}")
                return None
            logger.info(f"【123多盘】路径 {path} 未指定网盘，自动分配到 {account.name}")
            real = str(path)
        return self._get_folder_in(account, Path(real))

    def _get_folder_in(
        self, account: DiskAccount, path: Path
    ) -> Optional[schemas.FileItem]:
        """
        在指定网盘内获取目录，如目录不存在则创建

        :param account: 网盘账号
        :param path: 网盘内真实路径
        :return: 目录文件项，如果创建失败则返回None
        """
        def __find_dir(
            _fileitem: schemas.FileItem, _name: str
        ) -> Optional[schemas.FileItem]:
            """
            查找下级目录中匹配名称的目录
            """
            for sub_folder in self.list(_fileitem):
                if sub_folder.type != "dir":
                    continue
                if sub_folder.name == _name:
                    return sub_folder
            return None

        # 是否已存在（不存在则继续创建）
        try:
            folder = self._query_item(account, path)
            if folder:
                return folder
        except FileNotFoundError:
            pass
        # 逐级查找和创建目录
        fileitem = self._disk_root_item(account)
        for part in path.parts[1:]:
            dir_file = __find_dir(fileitem, part)
            if dir_file:
                fileitem = dir_file
            else:
                dir_file = self.create_folder(fileitem, part)
                if not dir_file:
                    logger.warn(f"【123多盘】创建目录 {fileitem.path}{part} 失败！")
                    return None
                fileitem = dir_file
        return fileitem

    # ==================== 查询 ====================

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None

        :param path: 文件或目录路径
        :return: 文件项，如果不存在则返回None
        """
        try:
            account, real = self._split(str(path))
            if account is None:
                return self._root_item() if str(path) == "/" else None
            return self._query_item(account, Path(real))
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.debug(f"【123多盘】获取文件信息失败: {str(e)}")
            return None

    def get_item_strict(self, path: Path) -> Optional[schemas.FileItem]:
        """
        严格获取文件或目录，无法确认状态时抛出存储查询异常

        :param path: 文件或目录路径
        :return: 文件项，确认不存在时返回 None
        :raises StorageQueryError: 网络或接口异常导致无法确认文件状态
        """
        try:
            account, real = self._split(str(path))
            if account is None:
                return self._root_item() if str(path) == "/" else None
            return self._query_item(account, Path(real))
        except FileNotFoundError:
            return None
        except StorageQueryError:
            raise
        except Exception as e:
            raise StorageQueryError(f"【123多盘】查询文件信息失败: {path} - {e}") from e

    def _query_item(
        self, account: DiskAccount, path: Path
    ) -> Optional[schemas.FileItem]:
        """
        查询指定网盘内的文件项

        :param account: 网盘账号
        :param path: 网盘内真实路径
        :return: 查询到的文件项
        :raises FileNotFoundError: 文件或目录确认不存在
        """
        if str(path) == "/":
            return self._disk_root_item(account)
        path_str = str(path).replace("\\", "/")
        file_id = self._path_to_id(account, path_str)
        if not file_id:
            return None
        resp = account.client.fs_info(int(file_id))
        check_response(resp)
        logger.debug(f"【123多盘】获取文件信息: {resp}")
        data = resp["data"]["infoList"][0]
        return schemas.FileItem(
            storage=self._disk_name,
            fileid=str(data["FileId"]),
            path=f"/{account.name}{path_str}" + ("/" if data["Type"] == 1 else ""),
            type="file" if data["Type"] == 0 else "dir",
            name=data["FileName"],
            basename=Path(data["FileName"]).stem,
            extension=Path(data["FileName"]).suffix[1:] if data["Type"] == 0 else None,
            pickcode=str(data),
            size=data["Size"] if data["Type"] == 0 else None,
            modify_time=int(
                datetime.fromisoformat(data["UpdateAt"]).timestamp()
            ),
        )

    def get_parent(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取父目录

        :param fileitem: 文件项
        :return: 父目录文件项，如果不存在则返回None
        """
        parent = str(Path(fileitem.path).parent)
        if parent == "/":
            return self._root_item()
        return self.get_item(Path(parent))

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情

        :param fileitem: 文件项
        :return: 包含详细信息的文件项，如果获取失败则返回None
        """
        return self.get_item(Path(fileitem.path))

    def exists(self, fileitem: schemas.FileItem) -> bool:
        """
        判断文件或目录是否存在
        """
        return True if self.get_item(Path(fileitem.path)) else False

    # ==================== 删除 / 重命名 ====================

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件或目录
        此操作将文件移动到回收站，不会永久删除

        :param fileitem: 要删除的文件项
        :return: 删除成功返回True，失败返回False
        """
        account, _ = self._split(fileitem.path)
        if account is None:
            return False
        try:
            resp = account.client.fs_trash(
                int(fileitem.fileid), event="intoRecycle"
            )
            check_response(resp)
            logger.debug(f"【123多盘】{account.name} 删除文件: {resp}")
            account.clear_cache()
            self._invalidate_usage(account)
            return True
        except Exception:
            return False

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件或目录

        :param fileitem: 要重命名的文件项
        :param name: 新名称
        :return: 重命名成功返回True，失败返回False
        """
        account, _ = self._split(fileitem.path)
        if account is None:
            return False
        try:
            payload = {
                "FileId": int(fileitem.fileid),
                "fileName": name,
                "duplicate": 2,
            }
            resp = account.client.fs_rename(payload)
            check_response(resp)
            logger.debug(f"【123多盘】{account.name} 重命名文件: {resp}")
            account.clear_cache()
            return True
        except Exception:
            return False

    # ==================== 下载 ====================

    def download(
        self, fileitem: schemas.FileItem, path: Path = None
    ) -> Optional[Path]:
        """
        下载文件，保存到本地，返回本地临时文件地址

        :param fileitem: 要下载的文件项
        :param path: 文件保存路径，如果为None则保存到临时目录
        :return: 下载成功返回本地文件路径，失败返回None
        """
        account, _ = self._split(fileitem.path)
        if account is None:
            logger.error(f"【123多盘】无法下载虚拟路径: {fileitem.path}")
            return None
        json_obj = ast.literal_eval(fileitem.pickcode)
        s3keyflag = json_obj["S3KeyFlag"]
        file_id = fileitem.fileid
        file_name = fileitem.name
        _md5 = json_obj["Etag"]
        size = json_obj["Size"]
        try:
            payload = {
                "Etag": _md5,
                "FileID": int(file_id),
                "FileName": file_name,
                "S3KeyFlag": s3keyflag,
                "Size": int(size),
            }
            # 换取并验证下载直链（通道被风控时自动切换换链域名）
            download_url = account.client.get_download_url(payload)
            if not download_url:
                logger.error(
                    f"【123多盘】获取下载链接失败（所有换链通道被风控或不可用）: {fileitem.name}"
                )
                return None
            local_path = (path or settings.TEMP_PATH) / fileitem.name
        except Exception as e:
            logger.error(f"【123多盘】获取下载链接失败: {fileitem.name} - {str(e)}")
            return None

        # 获取文件大小
        file_size = fileitem.size

        # 初始化进度条
        logger.info(f"【123多盘】开始下载: {fileitem.path} -> {local_path}")
        progress_callback = transfer_process(Path(fileitem.path).as_posix())

        try:
            with requests.get(download_url, stream=True) as r:
                r.raise_for_status()
                downloaded_size = 0

                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=10 * 1024 * 1024):
                        if global_vars.is_transfer_stopped(fileitem.path):
                            logger.info(f"【123多盘】{fileitem.path} 下载已取消！")
                            return None
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            # 更新进度
                            if file_size:
                                progress = (downloaded_size * 100) / file_size
                                progress_callback(progress)

                # 完成下载
                progress_callback(100)
                logger.info(f"【123多盘】下载完成: {fileitem.name}")

        except requests.exceptions.RequestException as e:
            logger.error(f"【123多盘】下载网络错误: {fileitem.name} - {str(e)}")
            if local_path.exists():
                local_path.unlink()
            return None
        except Exception as e:
            logger.error(f"【123多盘】下载失败: {fileitem.name} - {str(e)}")
            if local_path.exists():
                local_path.unlink()
            return None

        return local_path

    # ==================== 上传（含自动切换网盘） ====================

    @staticmethod
    def _md5(filepath: Path) -> str:
        """计算文件 MD5"""
        hash_md5 = md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def upload(
        self,
        target_dir: schemas.FileItem,
        local_path: Path,
        new_name: Optional[str] = None,
    ) -> Optional[schemas.FileItem]:
        """
        上传文件到云盘
        支持秒传、分块上传和普通上传，自动根据文件大小选择上传方式；
        目标盘空间不足时自动切换到剩余空间最大的网盘（需开启自动切换）

        :param target_dir: 上传目标目录项
        :param local_path: 本地文件路径
        :param new_name: 上传后的文件名，如果为None则使用本地文件名
        :return: 上传成功返回文件项，失败返回None
        """
        target_name = new_name or local_path.name
        file_size = local_path.stat().st_size

        logger.debug(f"【123多盘】{local_path} 开始计算 md5 值...")
        file_md5 = self._md5(local_path)

        # 解析目标网盘；未指定网盘时自动选择剩余空间最大的网盘
        account, real_dir = self._split(target_dir.path)
        if account is None:
            account = self._pick_disk(need_size=file_size)
            if not account:
                logger.error(
                    f"【123多盘】没有可用网盘上传 {target_name}（{StringUtils.str_filesize(file_size)}）"
                )
                return None
            dir_item = self._get_folder_in(
                account, Path(real_dir if real_dir != "/" else "/")
            )
            if not dir_item:
                logger.error(
                    f"【123多盘】在 {account.name} 创建目录失败: {real_dir}"
                )
                return None
            target_dir = dir_item
            logger.info(
                f"【123多盘】目标路径未指定网盘，自动分配到 {account.name}: {target_dir.path}"
            )

        return self._do_upload(
            account, target_dir, local_path, target_name, file_size, file_md5,
            rel_dir=real_dir,
        )

    def _do_upload(
        self,
        account: DiskAccount,
        target_dir: schemas.FileItem,
        local_path: Path,
        target_name: str,
        file_size: int,
        file_md5: str,
        rel_dir: str,
    ) -> Optional[schemas.FileItem]:
        """
        执行上传（含空间不足自动切换）

        :param account: 目标网盘账号
        :param target_dir: 目标目录项
        :param rel_dir: 目标目录在网盘内的真实路径（用于切换网盘后重建目录结构）
        """
        target_path = str(Path(target_dir.path) / target_name).replace("\\", "/")

        try:
            # 秒传文件
            resp = account.client.upload_request(
                {
                    "etag": file_md5,
                    "fileName": target_name,
                    "size": file_size,
                    "parentFileId": int(target_dir.fileid),
                    "type": 0,
                    "duplicate": 2,
                }
            )
            check_response(resp)
            if resp.get("data").get("Reuse"):
                logger.info(f"【123多盘】{account.name} {target_name} 秒传成功")
                logger.debug(resp)
                data = resp.get("data", {}).get("Info", {})
                self._invalidate_usage(account)
                return schemas.FileItem(
                    storage=self._disk_name,
                    fileid=str(data["FileId"]),
                    path=str(target_path) + ("/" if data["Type"] == 1 else ""),
                    type="file" if data["Type"] == 0 else "dir",
                    name=data["FileName"],
                    basename=Path(data["FileName"]).stem,
                    extension=Path(data["FileName"]).suffix[1:]
                    if data["Type"] == 0
                    else None,
                    pickcode=str(data),
                    size=data["Size"] if data["Type"] == 0 else None,
                    modify_time=int(
                        datetime.fromisoformat(data["UpdateAt"]).timestamp()
                    ),
                )
        except Exception as e:
            logger.error(f"【123多盘】{account.name} {target_name} 秒传出现未知错误：{e}")
            return None

        # 需要实际上传时检查空间，不足则自动切换网盘
        if self._auto_switch and not self._has_space(account, file_size):
            new_account = self._pick_disk(need_size=file_size, exclude=account)
            if new_account:
                # 在新网盘上重建目录结构
                new_dir = self._get_folder_in(new_account, Path(rel_dir or "/"))
                if new_dir:
                    logger.info(
                        f"【123多盘】{account.name} 空间不足（需 {StringUtils.str_filesize(file_size)}），"
                        f"自动切换到 {new_account.name} 上传 {target_name}"
                    )
                    return self._do_upload(
                        new_account, new_dir, local_path, target_name,
                        file_size, file_md5, rel_dir=rel_dir,
                    )
                else:
                    logger.warn(
                        f"【123多盘】切换到 {new_account.name} 后目录创建失败，尝试在原网盘上传"
                    )
            else:
                logger.error(
                    f"【123多盘】{account.name} 空间不足，且没有其他网盘能容纳 "
                    f"{target_name}（{StringUtils.str_filesize(file_size)}），上传失败"
                )
                return None

        try:
            # 上传信息
            upload_data = resp["data"]
            # 分块大小
            slice_size = int(upload_data["SliceSize"])

            upload_request_kwargs = {
                "method": "PUT",
                "headers": {"authorization": ""},
                "parse": ...,
                "timeout": 300,  # 设置5分钟超时
            }

            if file_size > slice_size:
                # 大文件分块上传
                logger.info(
                    f"【123多盘】{account.name} 开始上传: {local_path} -> {target_path}，"
                    f"分片大小：{StringUtils.str_filesize(slice_size)}"
                )
                # 初始化进度条
                progress_callback = transfer_process(local_path.as_posix())

                with open(local_path, "rb") as f:
                    slice_no = 1
                    offset = 0
                    for chunk in iter(lambda: f.read(slice_size), b""):
                        if global_vars.is_transfer_stopped(local_path.as_posix()):
                            logger.info(f"【123多盘】{local_path} 上传已取消！")
                            return None

                        if not chunk:
                            break

                        num_to_upload = min(slice_size, file_size - offset)

                        # 准备分片信息
                        upload_data["partNumberStart"] = slice_no
                        upload_data["partNumberEnd"] = slice_no + 1
                        upload_url_resp = account.client.upload_prepare(
                            upload_data,
                        )
                        check_response(upload_url_resp)

                        logger.info(
                            f"【123多盘】{account.name} 开始上传 {target_name} "
                            f"分片 {slice_no}: {offset} -> {offset + num_to_upload}"
                        )
                        logger.debug(f"{upload_url_resp} {upload_data}")

                        # 上传分片，失败时重试5次，每次重新获取上传URL
                        max_retries = 6
                        retry_count = 0
                        upload_success = False
                        current_upload_url_resp = upload_url_resp

                        while retry_count < max_retries and not upload_success:
                            try:
                                account.client.request(
                                    current_upload_url_resp["data"]["presignedUrls"][
                                        str(slice_no)
                                    ],
                                    data=chunk,
                                    **upload_request_kwargs,
                                )
                                upload_success = True
                            except Exception as upload_err:
                                retry_count += 1
                                if retry_count < max_retries:
                                    logger.warning(
                                        f"【123多盘】{account.name} {target_name} 分片 {slice_no} "
                                        f"上传失败，正在重试 ({retry_count}/{max_retries}): {upload_err}"
                                    )
                                    time.sleep(10)  # 等待10秒后重试

                                    # 重新获取上传URL
                                    try:
                                        logger.info(
                                            f"【123多盘】重新获取分片 {slice_no} 的上传URL"
                                        )
                                        current_upload_url_resp = (
                                            account.client.upload_prepare(
                                                upload_data,
                                            )
                                        )
                                        check_response(current_upload_url_resp)
                                    except Exception as url_err:
                                        logger.error(
                                            f"【123多盘】重新获取上传URL失败: {url_err}"
                                        )
                                        raise
                                else:
                                    logger.error(
                                        f"【123多盘】{account.name} {target_name} 分片 {slice_no} "
                                        f"上传失败，已达到最大重试次数: {upload_err}"
                                    )
                                    raise
                        slice_no += 1
                        offset += num_to_upload

                        # 更新进度
                        progress = (offset * 100) / file_size
                        progress_callback(progress)

                # 完成上传
                progress_callback(100)
            else:
                # 小文件直接上传
                logger.info(f"【123多盘】{account.name} 开始上传: {local_path} -> {target_path}")

                resp = account.client.upload_auth(
                    upload_data,
                )
                check_response(resp)

                # 上传文件，失败时重试6次，每次重新获取上传URL
                max_retries = 6
                retry_count = 0
                upload_success = False
                current_resp = resp

                with open(local_path, "rb") as f:
                    file_data = f.read()

                while retry_count < max_retries and not upload_success:
                    try:
                        account.client.request(
                            current_resp["data"]["presignedUrls"]["1"],
                            data=file_data,
                            **upload_request_kwargs,
                        )
                        upload_success = True
                    except Exception as upload_err:
                        retry_count += 1
                        if retry_count < max_retries:
                            logger.warning(
                                f"【123多盘】{account.name} {target_name} 上传失败，"
                                f"正在重试 ({retry_count}/{max_retries}): {upload_err}"
                            )
                            time.sleep(10)  # 等待10秒后重试

                            # 重新获取上传URL
                            try:
                                logger.info("【123多盘】重新获取上传URL")
                                current_resp = account.client.upload_auth(
                                    upload_data,
                                )
                                check_response(current_resp)
                            except Exception as url_err:
                                logger.error(f"【123多盘】重新获取上传URL失败: {url_err}")
                                raise
                        else:
                            logger.error(
                                f"【123多盘】{account.name} {target_name} 上传失败，"
                                f"已达到最大重试次数: {upload_err}"
                            )
                            raise

            upload_data["isMultipart"] = file_size > slice_size
            complete_resp = account.client.upload_complete(
                upload_data,
            )
            check_response(complete_resp)

            data = complete_resp.get("data", {}).get("file_info", {})
            self._invalidate_usage(account)
            return schemas.FileItem(
                storage=self._disk_name,
                fileid=str(data["FileId"]),
                path=str(target_path) + ("/" if data["Type"] == 1 else ""),
                type="file" if data["Type"] == 0 else "dir",
                name=data["FileName"],
                basename=Path(data["FileName"]).stem,
                extension=Path(data["FileName"]).suffix[1:]
                if data["Type"] == 0
                else None,
                pickcode=str(data),
                size=data["Size"] if data["Type"] == 0 else None,
                modify_time=int(
                    datetime.fromisoformat(data["UpdateAt"]).timestamp()
                ),
            )
        except Exception as e:
            logger.error(f"【123多盘】{account.name} {target_name} 上传出现未知错误：{e}")
            return None

    # ==================== 移动 / 复制（跨盘互传） ====================

    def move(
        self, fileitem: schemas.FileItem, path: Path, new_name: str
    ) -> bool:
        """
        移动文件或目录到目标位置
        同一网盘内使用服务端移动；跨网盘自动走 下载->上传->删除

        :param fileitem: 要移动的文件项
        :param path: 目标目录路径
        :param new_name: 移动后的新文件名
        :return: 移动成功返回True，失败返回False
        """
        src_account, src_real = self._split(fileitem.path)
        dst_account, dst_real = self._split(str(path))
        if src_account is None or dst_account is None:
            logger.error(f"【123多盘】无效的移动路径: {fileitem.path} -> {path}")
            return False
        if src_account is dst_account:
            # 同一网盘：服务端移动
            try:
                resp = src_account.client.fs_move(
                    fileitem.fileid,
                    parent_id=self._path_to_id(src_account, dst_real),
                )
                check_response(resp)
                logger.debug(f"【123多盘】{src_account.name} 移动文件: {resp}")
                new_real = f"{dst_real}{fileitem.name}" if dst_real.endswith("/") \
                    else f"{dst_real}/{fileitem.name}"
                # 更新缓存
                src_account.clear_cache()
                new_item = self._query_item(src_account, Path(new_real))
                if new_item and new_item.name != new_name:
                    self.rename(new_item, new_name)
                return True
            except Exception as e:
                logger.error(f"【123多盘】移动文件失败: {e}")
                return False
        # 跨网盘移动：下载->上传->删除
        logger.info(f"【123多盘】跨网盘移动: {fileitem.path} -> {path}")
        return self._cross_disk_copy(
            fileitem, Path(str(path)), new_name, keep_source=False
        )

    def copy(
        self, fileitem: schemas.FileItem, path: Path, new_name: str
    ) -> bool:
        """
        复制文件或目录到目标位置
        同一网盘内使用服务端复制；跨网盘自动走 下载->上传

        :param fileitem: 要复制的文件项
        :param path: 目标目录路径
        :param new_name: 复制后的新文件名
        :return: 复制成功返回True，失败返回False
        """
        src_account, src_real = self._split(fileitem.path)
        dst_account, dst_real = self._split(str(path))
        if src_account is None or dst_account is None:
            logger.error(f"【123多盘】无效的复制路径: {fileitem.path} -> {path}")
            return False
        if src_account is dst_account:
            # 同一网盘：服务端复制
            try:
                resp = src_account.client.fs_copy(
                    fileitem.fileid,
                    parent_id=self._path_to_id(src_account, dst_real),
                )
                check_response(resp)
                logger.debug(f"【123多盘】{src_account.name} 复制文件: {resp}")
                new_real = f"{dst_real}{fileitem.name}" if dst_real.endswith("/") \
                    else f"{dst_real}/{fileitem.name}"
                src_account.clear_cache()
                new_item = self._query_item(src_account, Path(new_real))
                if new_item and new_item.name != new_name:
                    self.rename(new_item, new_name)
                return True
            except Exception as e:
                logger.error(f"【123多盘】复制文件失败: {e}")
                return False
        # 跨网盘复制：下载->上传
        logger.info(f"【123多盘】跨网盘复制: {fileitem.path} -> {path}")
        return self._cross_disk_copy(
            fileitem, Path(str(path)), new_name, keep_source=True
        )

    def _cross_disk_copy(
        self,
        fileitem: schemas.FileItem,
        dst_parent_vpath: Path,
        new_name: str,
        keep_source: bool,
    ) -> bool:
        """
        跨网盘复制（keep_source=False 时为移动）

        :param fileitem: 源文件项
        :param dst_parent_vpath: 目标父目录虚拟路径
        :param new_name: 目标文件名
        :param keep_source: 是否保留源文件
        :return: 是否成功
        """
        try:
            if fileitem.type == "dir":
                # 目录：递归复制
                dst_dir = self.get_folder(dst_parent_vpath / new_name)
                if not dst_dir:
                    logger.error(
                        f"【123多盘】创建目标目录失败: {dst_parent_vpath / new_name}"
                    )
                    return False
                if not self._copy_dir_recursive(fileitem, dst_dir):
                    return False
            else:
                # 文件：下载后上传
                tmp_file = self.download(fileitem)
                if not tmp_file:
                    return False
                try:
                    dst_dir = self.get_folder(dst_parent_vpath)
                    if not dst_dir:
                        return False
                    new_item = self.upload(dst_dir, tmp_file, new_name)
                finally:
                    if tmp_file.exists():
                        tmp_file.unlink()
                if not new_item:
                    return False
            if not keep_source:
                self.delete(fileitem)
            return True
        except Exception as e:
            logger.error(
                f"【123多盘】跨网盘复制失败: {fileitem.path} -> {dst_parent_vpath} - {e}"
            )
            return False

    def _copy_dir_recursive(
        self, src_dir_item: schemas.FileItem, dst_dir_item: schemas.FileItem
    ) -> bool:
        """
        递归复制目录内容

        :param src_dir_item: 源目录项
        :param dst_dir_item: 目标目录项
        :return: 是否成功
        """
        try:
            for child in self.list(src_dir_item):
                if child.type == "dir":
                    new_dst = self.create_folder(dst_dir_item, child.name)
                    if not new_dst:
                        logger.error(f"【123多盘】创建目录失败: {child.name}")
                        return False
                    if not self._copy_dir_recursive(child, new_dst):
                        return False
                else:
                    tmp_file = self.download(child)
                    if not tmp_file:
                        return False
                    try:
                        if not self.upload(dst_dir_item, tmp_file, child.name):
                            return False
                    finally:
                        if tmp_file.exists():
                            tmp_file.unlink()
            return True
        except Exception as e:
            logger.error(f"【123多盘】目录复制失败: {e}")
            return False

    def _item_size(self, fileitem: schemas.FileItem) -> int:
        """
        递归统计文件或目录大小
        """
        if fileitem.type == "file":
            return fileitem.size or 0
        total = 0
        try:
            for child in self.list(fileitem):
                total += self._item_size(child)
        except Exception:
            pass
        return total

    # ==================== 快照 ====================

    def snapshot(
        self,
        path: Path,
        last_snapshot_time: float = None,
        max_depth: int = 5,
    ) -> Dict[str, Dict]:
        """
        快照存储，用于增量监控

        :param path: 路径
        :param last_snapshot_time: 上次快照时间，用于增量快照
        :param max_depth: 最大递归深度，避免过深遍历
        :return: 文件信息字典
        """
        account, real = self._split(str(path))
        if account:
            return self._snapshot_account(
                account, Path(real), last_snapshot_time, max_depth
            )
        # 虚拟路径（未指定网盘）：合并所有网盘中对应路径的快照
        result = {}
        for acc in self._accounts:
            result.update(
                self._snapshot_account(
                    acc, Path(str(path) if str(path) != "/" else "/"),
                    last_snapshot_time, max_depth,
                )
            )
        return result

    def _snapshot_account(
        self,
        account: DiskAccount,
        path: Path,
        last_snapshot_time: float,
        max_depth: int,
    ) -> Dict[str, Dict]:
        """
        快照单个网盘
        """
        files_info = {}
        _last_time = last_snapshot_time or 0

        def __snapshot_file(
            _fileitem: schemas.FileItem, current_depth: int = 0
        ):
            """
            递归获取文件信息
            """
            try:
                if _fileitem.type == "dir":
                    # 检查递归深度限制
                    if current_depth >= max_depth:
                        return
                    # 增量检查：如果目录修改时间早于上次快照，跳过
                    if (
                        self.snapshot_check_folder_modtime
                        and _last_time
                        and _fileitem.modify_time
                        and _fileitem.modify_time <= _last_time
                    ):
                        return
                    # 遍历子文件
                    sub_files = self.list(_fileitem)
                    for sub_file in sub_files:
                        __snapshot_file(sub_file, current_depth + 1)
                else:
                    # 记录文件的完整信息用于比对
                    if getattr(_fileitem, "modify_time", 0) > _last_time:
                        files_info[_fileitem.path] = {
                            "size": _fileitem.size or 0,
                            "modify_time": getattr(_fileitem, "modify_time", 0),
                            "type": _fileitem.type,
                        }
            except Exception as e:
                logger.debug(f"Snapshot error for {_fileitem.path}: {e}")

        fileitem = (
            self._query_item(account, path)
            if str(path) != "/"
            else self._disk_root_item(account)
        )
        if not fileitem:
            return {}
        __snapshot_file(fileitem)
        return files_info

    # ==================== 空间使用 ====================

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        获取存储使用情况（所有网盘合并）

        :return: 存储使用情况对象，包含总容量和可用容量，获取失败返回None
        """
        total = 0.0
        used = 0.0
        ok_count = 0
        for acc in self._accounts:
            usage = self._usage_of(acc)
            if not usage:
                continue
            total += usage[0]
            used += usage[1]
            ok_count += 1
        if not ok_count:
            return None
        return schemas.StorageUsage(
            total=total,
            available=total - used,
        )

    def usage_details(self, force: bool = False) -> Dict:
        """
        获取所有网盘空间使用明细

        :param force: 是否强制刷新缓存
        :return: {total, used, available, disks: [{name, total, used, available, ok, error}]}
        """
        disks = []
        total = 0.0
        used = 0.0
        for acc in self._accounts:
            usage = self._usage_of(acc, force=force)
            if usage:
                disks.append(
                    {
                        "name": acc.name,
                        "total": usage[0],
                        "used": usage[1],
                        "available": usage[0] - usage[1],
                        "ok": True,
                        "error": "",
                    }
                )
                total += usage[0]
                used += usage[1]
            else:
                disks.append(
                    {
                        "name": acc.name,
                        "total": 0,
                        "used": 0,
                        "available": 0,
                        "ok": False,
                        "error": "获取空间信息失败，请检查账号配置",
                    }
                )
        return {
            "total": total,
            "used": used,
            "available": total - used,
            "disks": disks,
        }

    def check(self) -> bool:
        """
        检查所有网盘连接是否正常

        :return: 全部正常返回True
        """
        for acc in self._accounts:
            if not self._usage_of(acc, force=True):
                return False
        return True

    def test(self) -> List[Dict]:
        """
        测试所有网盘连接

        :return: [{name, ok, message}]
        """
        results = []
        for acc in self._accounts:
            try:
                usage = self._usage_of(acc, force=True)
                if usage:
                    results.append(
                        {
                            "name": acc.name,
                            "ok": True,
                            "message": (
                                f"连接正常 · 已用 {StringUtils.str_filesize(usage[1])} / "
                                f"总 {StringUtils.str_filesize(usage[0])}"
                            ),
                        }
                    )
                else:
                    results.append(
                        {
                            "name": acc.name,
                            "ok": False,
                            "message": "获取空间信息失败，请检查手机号/密码",
                        }
                    )
            except Exception as e:
                results.append(
                    {
                        "name": acc.name,
                        "ok": False,
                        "message": f"连接失败: {e}",
                    }
                )
        return results

    # ==================== 一键均衡 ====================

    def balance(self, max_items: int = 20) -> Dict:
        """
        一键均衡：将空间紧张网盘中最旧的文件/目录移动到剩余空间最大的网盘

        :param max_items: 单次最多移动的条目数
        :return: 移动结果统计
        """
        moved = []
        total_moved = 0
        for acc in list(self._accounts):
            if total_moved >= max_items:
                break
            while total_moved < max_items:
                usage = self._usage_of(acc)
                if not usage:
                    break
                total, used = usage
                available = total - used
                if available >= self._reserve_size:
                    break
                target = self._pick_disk(exclude=acc)
                if not target:
                    logger.warn(
                        f"【123多盘】{acc.name} 空间紧张，但没有其他可用网盘可转移"
                    )
                    break
                # 取该网盘根目录下最旧的条目（按修改时间排序）
                candidates = [
                    item for item in self._list_account(acc, "/")
                    if item.type == "file"
                ]
                if not candidates:
                    logger.warn(
                        f"【123多盘】{acc.name} 根目录下没有可转移的文件"
                    )
                    break
                candidates.sort(key=lambda item: item.modify_time or 0)
                item = candidates[0]
                # 目标盘空间足够时才转移
                if not self._has_space(target, self._item_size(item)):
                    logger.warn(
                        f"【123多盘】{target.name} 空间不足，无法接收 {item.path}"
                    )
                    break
                dst_vpath = Path(f"/{target.name}/")
                if self.move(item, dst_vpath, item.name):
                    moved.append(
                        {
                            "from": item.path,
                            "to": f"/{target.name}/{item.name}",
                            "size": item.size or 0,
                        }
                    )
                    total_moved += 1
                    logger.info(
                        f"【123多盘】均衡: {item.path} -> /{target.name}/{item.name}"
                    )
                else:
                    logger.error(f"【123多盘】均衡失败: {item.path}")
                    break
        return {
            "moved": moved,
            "count": total_moved,
        }

    # ==================== 整理方式 ====================

    def support_transtype(self) -> dict:
        """
        支持的整理方式

        :return: 支持的整理方式字典
        """
        return self.transtype

    def is_support_transtype(self, transtype: str) -> bool:
        """
        是否支持整理方式

        :param transtype: 整理方式 (move/copy)
        :return: 是否支持
        """
        return transtype in self.transtype

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        硬链接文件
        云盘存储不支持硬链接操作

        :param fileitem: 文件项
        :param target_file: 目标文件路径
        :return: 始终返回False，表示不支持此操作
        """
        return False

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        软链接文件
        云盘存储不支持软链接操作

        :param fileitem: 文件项
        :param target_file: 目标文件路径
        :return: 始终返回False，表示不支持此操作
        """
        return False
