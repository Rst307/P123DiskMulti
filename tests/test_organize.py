"""
P123DiskMulti 定期目录整理模块独立测试（不依赖 MoviePilot 环境）

复用 test_logic.py 的内存模拟 123 API 环境，验证：
1. 递归遍历收集媒体文件（跳过字幕 / 蓝光原盘结构目录 / 空目录）
2. 逐个提交到 MoviePilot 整理队列（TransferChain.manual_transfer，background=True）
3. 失败文件记录与统计、目录不存在处理、注释行跳过
4. 整理链异常容错
5. 后台整理（start_organize / 状态 / 防并发 / 完成回调）

运行：python tests/test_organize.py
"""
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import test_logic as tl  # noqa: E402

import P123DiskMulti.organize as organize_mod  # noqa: E402

OrganizeRunner = organize_mod.OrganizeRunner
P123MultiApi = tl.P123MultiApi
make_account = tl.make_account

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


# ---- 假整理链（stub app.chain.transfer）----
class FakeTransferChain:
    """记录 manual_transfer 调用，可按路径模拟失败 / 异常。"""

    calls = []
    fail_paths = set()
    raise_error = False
    delay = 0.0

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.fail_paths = set()
        cls.raise_error = False
        cls.delay = 0.0

    def manual_transfer(self, fileitem=None, background=None, **kwargs):
        if self.delay:
            time.sleep(self.delay)
        if self.raise_error:
            raise RuntimeError("整理链炸了")
        path = fileitem.path if fileitem else ""
        if path in self.fail_paths:
            return False, "未匹配到整理目录"
        self.calls.append((fileitem, background, kwargs))
        return True, ""


_transfer_mod = types.ModuleType("app.chain.transfer")
_transfer_mod.TransferChain = FakeTransferChain
sys.modules["app.chain.transfer"] = _transfer_mod

# 确保假 app 包可导入
_chain_pkg = types.ModuleType("app.chain")
_chain_pkg.__path__ = []
sys.modules.setdefault("app.chain", _chain_pkg)


def build_env():
    """构造 /盘A/整理 目录树（含媒体/字幕/子目录/BDMV/空目录）。"""
    api = P123MultiApi(disks=[], disk_name="123云盘")
    acc_a = make_account("盘A", 100 * 1024 ** 3, 10 * 1024 ** 3)
    api._accounts = [acc_a]
    fake = acc_a.client._fake
    root = fake._entry("整理", 0, "dir")
    fake._entry("阿凡达.mkv", root["FileId"], "file", size=1000, etag="m1")
    fake._entry("剧集 S01E01.mkv", root["FileId"], "file", size=2000, etag="m2")
    fake._entry("字幕.srt", root["FileId"], "file", size=10, etag="m3")
    sub = fake._entry("子目录", root["FileId"], "dir")
    fake._entry("内.mp4", sub["FileId"], "file", size=3000, etag="m4")
    bd = fake._entry("BDMV", root["FileId"], "dir")
    fake._entry("index.bdmv", bd["FileId"], "file", size=4000, etag="m5")
    fake._entry("空目录", root["FileId"], "dir")
    return api, acc_a, fake


def test_walk_collects_only_media():
    print("\n[1] 遍历收集：只收视频，跳过字幕/BDMV/空目录")
    api, _, _ = build_env()
    runner = OrganizeRunner(api=api)
    root_item = api.get_item("/盘A/整理")
    files = list(runner._walk_media_files(root_item))
    paths = sorted(f.path for f in files)
    check(len(files) == 3, f"收集到 3 个媒体文件，实际 {len(files)}: {paths}")
    check(paths == ["/盘A/整理/剧集 S01E01.mkv", "/盘A/整理/子目录/内.mp4",
                    "/盘A/整理/阿凡达.mkv"],
          "路径正确（含子目录递归，无字幕/BDMV）")
    check(all(f.type == "file" for f in files), "全部为文件")


def test_submit_all_files_to_transfer_chain():
    print("\n[2] 整理提交：全部媒体文件进入 MoviePilot 整理队列")
    api, _, _ = build_env()
    FakeTransferChain.reset()
    runner = OrganizeRunner(api=api)
    result = runner._organize_unlocked("/盘A/整理\n")
    check(result["ok"] == 3, f"提交 3 个，实际 {result['ok']}")
    check(result["fail"] == 0, f"失败 0 个，实际 {result['fail']}")
    check(result["paths"] == ["/盘A/整理"], "记录处理目录")
    check(len(FakeTransferChain.calls) == 3, "TransferChain 被调用 3 次")
    for item, bg, kwargs in FakeTransferChain.calls:
        check(item.storage == "123云盘", f"storage=123云盘: {item.path}")
        check(bg is True, f"background=True: {item.path}")
    paths = sorted(c[0].path for c in FakeTransferChain.calls)
    check(paths == sorted(["/盘A/整理/阿凡达.mkv", "/盘A/整理/剧集 S01E01.mkv",
                           "/盘A/整理/子目录/内.mp4"]),
          "提交的文件路径正确")


def test_failed_files_recorded():
    print("\n[3] 失败文件：记录错误且不影响其他文件")
    api, _, _ = build_env()
    FakeTransferChain.reset()
    FakeTransferChain.fail_paths = {"/盘A/整理/阿凡达.mkv"}
    runner = OrganizeRunner(api=api)
    result = runner._organize_unlocked("/盘A/整理")
    check(result["ok"] == 2, f"成功 2 个，实际 {result['ok']}")
    check(result["fail"] == 1, f"失败 1 个，实际 {result['fail']}")
    check(any("阿凡达.mkv" in e and "未匹配到整理目录" in e for e in result["errors"]),
          "错误信息包含文件路径与原因")


def test_missing_dir_and_comment_lines():
    print("\n[4] 目录不存在 / 注释行 / 空行处理")
    api, _, _ = build_env()
    FakeTransferChain.reset()
    runner = OrganizeRunner(api=api)
    result = runner._organize_unlocked(
        "# 注释行\n\n/盘A/整理\n/盘B/不存在的目录\n"
    )
    check(result["ok"] == 3, f"有效目录正常整理: {result['ok']}")
    check(result["fail"] == 1, f"无效目录记 1 失败: {result['fail']}")
    check(any("目录不存在或不是文件夹" in e and "/盘B/不存在的目录" in e
              for e in result["errors"]), "无效目录错误信息正确")
    check(result["paths"] == ["/盘A/整理"], "paths 只含有效目录")


def test_empty_dir():
    print("\n[5] 空目录：无媒体文件可整理")
    api, _, _ = build_env()
    api._accounts[0].client._fake._entry("空整理", 0, "dir")
    FakeTransferChain.reset()
    runner = OrganizeRunner(api=api)
    result = runner._organize_unlocked("/盘A/空整理")
    check(result["ok"] == 0 and result["fail"] == 0, "空目录 0 提交 0 失败")
    check(FakeTransferChain.calls == [], "TransferChain 未被调用")


def test_chain_exception_tolerated():
    print("\n[6] 整理链异常：单文件失败不中断整体")
    api, _, _ = build_env()
    FakeTransferChain.reset()
    FakeTransferChain.raise_error = True
    runner = OrganizeRunner(api=api)
    result = runner._organize_unlocked("/盘A/整理")
    check(result["ok"] == 0 and result["fail"] == 3, f"全部记为失败: {result}")
    check(any("整理链调用异常" in e for e in result["errors"]), "异常信息被记录")


def test_custom_media_exts():
    print("\n[7] 自定义媒体扩展名")
    api, _, _ = build_env()
    FakeTransferChain.reset()
    runner = OrganizeRunner(api=api, media_exts=["iso"])
    root_item = api.get_item("/盘A/整理")
    files = list(runner._walk_media_files(root_item))
    check(files == [], "iso 过滤器下无文件（BDMV 已被跳过）")


def test_background_start_status_and_callback():
    print("\n[8] 后台整理：立即返回 / 防并发 / 状态 / 完成回调")
    api, _, _ = build_env()
    FakeTransferChain.reset()
    FakeTransferChain.delay = 0.3  # 放慢后台任务，留出状态检查窗口
    runner = OrganizeRunner(api=api)
    done_box = {}

    def on_done(result):
        done_box["result"] = result

    started = runner.start_organize("/盘A/整理", on_done=on_done)
    check(started is True, "首次启动成功")
    started2 = runner.start_organize("/盘A/整理")
    check(started2 is False, "运行中拒绝再次启动")
    st = runner.organize_status()
    check(st["running"] is True, "状态显示运行中")

    deadline = time.time() + 10
    while runner.organize_status().get("running") and time.time() < deadline:
        time.sleep(0.05)
    st = runner.organize_status()
    check(st["running"] is False, "后台整理已结束")
    check(st["last_result"]["ok"] == 3, f"后台结果 ok=3: {st['last_result']}")
    check(st["last_result"]["fail"] == 0, "后台结果 fail=0")
    check(done_box.get("result", {}).get("ok") == 3, "完成回调收到结果")
    check(len(FakeTransferChain.calls) == 3,
          f"后台模式 TransferChain 被调用 3 次，实际 {len(FakeTransferChain.calls)}")
    FakeTransferChain.delay = 0
    check(runner.start_organize("/盘A/整理") is True, "结束后可再次启动")
    deadline = time.time() + 10
    while runner.organize_status().get("running") and time.time() < deadline:
        time.sleep(0.05)


def test_transfer_failed_better_file_deletion():
    print("\n[9] 整理失败「质量更好」：自动删除网盘低版本源文件")
    api, _, _ = build_env()
    runner = OrganizeRunner(api=api)
    check(api.get_item("/盘A/整理/阿凡达.mkv") is not None, "源文件初始存在")

    # 1. 质量更好 + 123盘源 → 删除（移入回收站）
    ev = {
        "fileitem": SimpleNamespace(
            storage="123云盘", path="/盘A/整理/阿凡达.mkv"
        ),
        "transferinfo": SimpleNamespace(
            success=False, message="媒体库存在同名文件，且质量更好"
        ),
    }
    ok = runner.handle_transfer_failed(ev)
    check(ok is True, "质量更好失败 → 删除成功")
    check(api.get_item("/盘A/整理/阿凡达.mkv") is None, "网盘源文件已删除")
    check(runner.organize_status()["deleted"] == 1, "deleted 累计为 1")

    # 2. 成功事件 → 不删
    ev2 = {
        "fileitem": SimpleNamespace(
            storage="123云盘", path="/盘A/整理/剧集 S01E01.mkv"
        ),
        "transferinfo": SimpleNamespace(
            success=True, message="媒体库存在同名文件，且质量更好"
        ),
    }
    ok2 = runner.handle_transfer_failed(ev2)
    check(ok2 is False, "成功事件不处理")
    check(api.get_item("/盘A/整理/剧集 S01E01.mkv") is not None, "成功事件源文件保留")

    # 3. 其他失败原因 → 不删
    ev3 = {
        "fileitem": SimpleNamespace(
            storage="123云盘", path="/盘A/整理/剧集 S01E01.mkv"
        ),
        "transferinfo": SimpleNamespace(success=False, message="磁盘空间不足"),
    }
    ok3 = runner.handle_transfer_failed(ev3)
    check(ok3 is False, "其他失败原因不处理")
    check(api.get_item("/盘A/整理/剧集 S01E01.mkv") is not None, "其他原因源文件保留")

    # 4. 非 123 盘源 → 不删
    ev4 = {
        "fileitem": SimpleNamespace(storage="local", path="/download/阿凡达.mkv"),
        "transferinfo": SimpleNamespace(
            success=False, message="媒体库存在同名文件，且质量更好"
        ),
    }
    ok4 = runner.handle_transfer_failed(ev4)
    check(ok4 is False, "非 123 盘源不处理")

    # 5. 源文件已不存在 → 不删不报错
    ev5 = {
        "fileitem": SimpleNamespace(
            storage="123云盘", path="/盘A/整理/子目录/内.mp4"
        ),
        "transferinfo": SimpleNamespace(
            success=False, message="媒体库存在同名文件，且质量更好"
        ),
    }
    api.delete(api.get_item("/盘A/整理/子目录/内.mp4"))
    ok5 = runner.handle_transfer_failed(ev5)
    check(ok5 is False, "源文件已不存在则不处理")

    # 6. 事件数据不是 dict / 缺字段 → 容错
    check(runner.handle_transfer_failed(None) is False, "None 事件数据容错")
    check(runner.handle_transfer_failed({"fileitem": None}) is False, "缺 transferinfo 容错")

    # 7. 删除失败 → 不计数
    ev7 = {
        "fileitem": SimpleNamespace(
            storage="123云盘", path="/盘A/整理/剧集 S01E01.mkv"
        ),
        "transferinfo": SimpleNamespace(
            success=False, message="媒体库存在同名文件，且质量更好"
        ),
    }
    check(api.get_item("/盘A/整理/剧集 S01E01.mkv") is not None, "源文件仍在")
    api.delete = lambda fileitem: False  # 模拟删除失败
    ok7 = runner.handle_transfer_failed(ev7)
    check(ok7 is False, "删除失败返回 False")
    check(runner.organize_status()["deleted"] == 1, "删除失败不计数（仍为 1）")


def test_main():
    test_walk_collects_only_media()
    test_submit_all_files_to_transfer_chain()
    test_failed_files_recorded()
    test_missing_dir_and_comment_lines()
    test_empty_dir()
    test_chain_exception_tolerated()
    test_custom_media_exts()
    test_background_start_status_and_callback()
    test_transfer_failed_better_file_deletion()
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    test_main()
