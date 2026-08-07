"""
P123DiskMulti 分享增量同步模块独立测试（不依赖 MoviePilot 环境）

复用 test_logic.py 的内存模拟 123 API 环境，验证：
1. 分享内容检查（可访问性 / 文件与目录统计 / 非法文件名拒绝）
2. 增量转存：服务器端直传、目录结构保留、已转存跳过
3. pending 确认机制（异步转存未完成 → 下轮确认/重转）
4. 后台转存（start_sync / 状态 / 防并发）
5. 分享链接 URL 解析与指纹（密码不入指纹）
6. 目标目录盘前缀校验

运行：python tests/test_share.py
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_logic as tl  # noqa: E402

import P123DiskMulti.share as share_mod  # noqa: E402

ShareSync = share_mod.ShareSync
ShareDB = share_mod.ShareDB
share_fingerprint = share_mod.share_fingerprint
file_key = share_mod.file_key
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


def build_env():
    """构造 盘A + 一个分享树（/电影/阿凡达.mkv + /电影/字幕.srt + /剧集/E01.mkv + 子目录）。"""
    api = P123MultiApi(disks=[], disk_name="123云盘")
    acc_a = make_account("盘A", 100 * 1024 ** 3, 10 * 1024 ** 3)
    api._accounts = [acc_a]
    fake = acc_a.client._fake
    fake.share_entries = {
        0: [
            {"FileId": 9001, "FileName": "电影", "ParentFileId": 0, "Type": 1,
             "Size": 0, "Etag": "", "S3KeyFlag": ""},
            {"FileId": 9004, "FileName": "剧集", "ParentFileId": 0, "Type": 1,
             "Size": 0, "Etag": "", "S3KeyFlag": ""},
        ],
        9001: [
            {"FileId": 9002, "FileName": "阿凡达.mkv", "ParentFileId": 9001, "Type": 0,
             "Size": 1000, "Etag": "m1", "S3KeyFlag": "s3-1"},
            {"FileId": 9003, "FileName": "字幕.srt", "ParentFileId": 9001, "Type": 0,
             "Size": 10, "Etag": "m2", "S3KeyFlag": "s3-2"},
        ],
        9004: [
            {"FileId": 9005, "FileName": "第一季", "ParentFileId": 9004, "Type": 1,
             "Size": 0, "Etag": "", "S3KeyFlag": ""},
        ],
        9005: [
            {"FileId": 9006, "FileName": "E01.mkv", "ParentFileId": 9005, "Type": 0,
             "Size": 2000, "Etag": "m3", "S3KeyFlag": "s3-3"},
        ],
    }
    return api, acc_a, fake


db_file = Path(tempfile.mkdtemp(prefix="p123share_")) / "test.sqlite3"

# ============ 1. 分享指纹与身份键 ============
print("== 1. 分享指纹与身份键 ==")
fp1 = share_fingerprint("https://www.123pan.com/s/AbC123-DEF?pwd=xyz")
fp2 = share_fingerprint("https://www.123pan.com/s/AbC123-DEF?pwd=other")
fp3 = share_fingerprint("https://www.123pan.com/s/AbC123-DEF")
check(fp1 == fp3, "指纹不含提取码（改密码指纹不变）")
check(fp2 == fp3, "URL 带/不带 pwd 指纹一致")
check(share_fingerprint("") == "", "空分享指纹为空")
check(share_fingerprint("AbC123-DEF") == share_fingerprint("AbC123-DEF"), "纯分享码指纹稳定")
k1 = file_key(fp1, "9002", "m1", 1000)
k2 = file_key(fp1, "9002", "m1", 1001)
check(k1 != k2, "身份键含大小")
check(file_key(fp1, "9002", "m1", 1000) == k1, "身份键稳定")

# ============ 2. 内容检查 ============
print("== 2. 分享内容检查 ==")
api, acc_a, fake = build_env()
task = ShareSync(
    api=api, task_id="t1", name="测试分享",
    share_key="https://www.123pan.com/s/AbC123-DEF?pwd=xyz",
    share_pwd="", target_vpath="/盘A/分享", db_path=db_file,
)
check(task.share_key == "AbC123-DEF" and "pwd" not in task.share_key, f"URL 分享码已规范化为纯分享码: {task.share_key}")
result = task.check()
check(result["success"] is True, "分享可访问")
check(result["files"] == 3 and result["dirs"] == 3, f"统计正确: {result['files']} 文件 {result['dirs']} 目录")
check(result["total_size"] == 3010, f"总大小正确: {result['total_size']}")
check(len(result["root_items"]) == 2, "根目录项 2 个（电影/剧集）")

# ============ 3. 首次增量转存 ============
print("== 3. 首次增量转存 ==")
result = task.sync()
print(f"  (结果: scanned={result['scanned']} copied={result['copied']} skipped={result['skipped']} failed={result['failed']} pending={result['pending']})")
check(result["success"] is True, "转存成功")
check(result["scanned"] == 3 and result["copied"] == 3 and result["skipped"] == 0, "3 个新文件全部转存")
check(fake._has_dir("分享"), "目标根目录「分享」已创建")
# 目录结构保留：/电影/阿凡达.mkv
movies = next((e for e in fake.entries.values() if e["FileName"] == "电影" and e["ParentFileId"] != 9001), None)
check(movies is not None, "分享内目录结构保留（目标盘有「电影」目录）")
av = next((e for e in fake.entries.values() if e["FileName"] == "阿凡达.mkv" and e["ParentFileId"] == movies["FileId"]), None)
check(av is not None and av["Size"] == 1000 and av["Etag"] == "m1", "文件转存且元数据一致")
seasons = next((e for e in fake.entries.values() if e["FileName"] == "第一季"), None)
e01 = next((e for e in fake.entries.values() if e["FileName"] == "E01.mkv" and e["ParentFileId"] == seasons["FileId"]), None)
check(e01 is not None, "多级目录转存正确")
check(task.status()["synced"] == 3, "DB 记录 3 个文件")

# ============ 4. 第二次转存：全部跳过 ============
print("== 4. 增量去重 ==")
result = task.sync()
check(result["copied"] == 0 and result["skipped"] == 3, f"已转存全部跳过: copied={result['copied']} skipped={result['skipped']}")

# ============ 5. 分享新增文件后只转存新文件 ============
print("== 5. 只转存新文件 ==")
fake.share_entries[9001].append(
    {"FileId": 9007, "FileName": "新电影.mkv", "ParentFileId": 9001, "Type": 0,
     "Size": 3000, "Etag": "m4", "S3KeyFlag": "s3-4"}
)
result = task.sync()
check(result["copied"] == 1 and result["skipped"] == 3, f"只转存新增文件: copied={result['copied']} skipped={result['skipped']}")

# ============ 6. pending 确认与重转 ============
print("== 6. pending 确认与重转 ==")
fake.share_entries[9001].append(
    {"FileId": 9008, "FileName": "延迟文件.mkv", "ParentFileId": 9001, "Type": 0,
     "Size": 500, "Etag": "m5", "S3KeyFlag": "s3-5"}
)
fake.defer_share_copy = True
result = task.sync()
check(result["pending"] == 1 and result["copied"] == 0, f"异步未完成 → pending: pending={result['pending']}")
fake.defer_share_copy = False
result = task.sync()
check(result["copied"] == 1 and result["pending"] == 0, f"下轮确认失败后重转成功: copied={result['copied']}")
check(task.status()["synced"] == 5, "DB 记录 5 个文件")

# ============ 7. 转存失败路径 ============
print("== 7. 转存失败 ==")
fake.share_entries[9001].append(
    {"FileId": 9009, "FileName": "失败文件.mkv", "ParentFileId": 9001, "Type": 0,
     "Size": 100, "Etag": "m6", "S3KeyFlag": "s3-6"}
)
fake.fail_share_copy = True
result = task.sync()
check(result["failed"] == 1, f"转存请求失败计入 failed: failed={result['failed']}")
fake.fail_share_copy = False
result = task.sync()
check(result["copied"] == 1, "故障恢复后重转成功")

# ============ 8. 非法文件名拒绝 ============
print("== 8. 非法文件名拒绝 ==")
fake.share_entries[9001].append(
    {"FileId": 9010, "FileName": "恶意/路径.mkv", "ParentFileId": 9001, "Type": 0,
     "Size": 1, "Etag": "m7", "S3KeyFlag": "s3-7"}
)
result = task.check()
check(result["success"] is False, "非法文件名导致检查失败（拒绝恶意分享）")
fake.share_entries[9001] = [e for e in fake.share_entries[9001] if e["FileId"] != 9010]

# ============ 9. 后台转存 ============
print("== 9. 后台转存 ==")
done_flag = {}


def on_done(res):
    done_flag["result"] = res


fake.share_entries[9001].append(
    {"FileId": 9011, "FileName": "后台文件.mkv", "ParentFileId": 9001, "Type": 0,
     "Size": 700, "Etag": "m8", "S3KeyFlag": "s3-8"}
)
started = task.start_sync(on_done=on_done)
check(started is True, "start_sync 立即返回 True")
check(task.start_sync() is False, "运行中再次启动被拒绝")
busy = task.sync()
check("已有转存任务" in busy.get("errors", [""])[0], "运行中同步调用返回忙碌错误")
waited = 0
while task.status()["running"] and waited < 150:
    time.sleep(0.1)
    waited += 1
st = task.status()
check(not st["running"], "后台转存完成")
check(st["last_result"] is not None and st["last_result"]["copied"] == 1, "状态含上次结果且正确")
check("result" in done_flag and done_flag["result"]["copied"] == 1, "完成回调被调用")
masked = str(st["share_key"])
check("****" in masked and "wd=xyz" not in masked and "AbC123-DEF" not in masked, f"分享标识脱敏: {masked}")

# ============ 10. 目标目录校验 ============
print("== 10. 目标目录校验 ==")
try:
    ShareSync(api=api, task_id="bad", name="坏配置", share_key="AbC123",
              share_pwd="", target_vpath="/分享", db_path=db_file)
    check(False, "无盘前缀目标目录应被拒绝")
except ValueError:
    check(True, "无盘前缀目标目录被拒绝")

task.close()
print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
