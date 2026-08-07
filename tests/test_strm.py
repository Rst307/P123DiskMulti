"""
P123DiskMulti STRM 功能独立测试（不依赖 MoviePilot 环境）

复用 test_logic.py 的内存模拟 123 API 环境，验证：
1. STRM URL 构建（302 端点 + 文件参数）
2. 302 换下载地址（带/不带 S3KeyFlag，秒传转存兜底）
3. 全量同步：多盘扫描、非媒体跳过、蓝光目录跳过、覆盖模式
4. 监控整理事件：路径匹配、蓝光跳过、非媒体跳过
5. 路径映射解析与匹配

运行：python tests/test_strm.py
"""
import sys
import tempfile
from pathlib import Path

# 复用 test_logic 的模拟环境（含 __main__ 保护，导入不会执行测试）
sys.path.insert(0, str(Path(__file__).parent))
import test_logic as tl  # noqa: E402

# 补齐 STRM 测试需要的最小环境
tl.settings.API_TOKEN = "test-api-token"

# 通过 test_logic 注册的包路径导入 strm 模块
import P123DiskMulti.strm as strm_mod  # noqa: E402

StrmHelper = strm_mod.StrmHelper
P123MultiApi = tl.P123MultiApi
FileItem = tl.FileItem
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


# ============ 构造测试环境 ============
api = P123MultiApi(disks=[], disk_name="123云盘")
acc_a = make_account("盘A", 100 * 1024 ** 3, 10 * 1024 ** 3)
acc_b = make_account("盘B", 200 * 1024 ** 3, 50 * 1024 ** 3)
api._accounts = [acc_a, acc_b]

fake_a = acc_a.client._fake
fake_b = acc_b.client._fake
# 盘A：/电影/阿凡达.mkv + 字幕 + 文本 + 蓝光目录
movies_a = fake_a._entry("电影", 0, "dir")
fake_a._entry("阿凡达.mkv", movies_a["FileId"], "file", size=1000, etag="md5-av")
fake_a._entry("阿凡达.srt", movies_a["FileId"], "file", size=10, etag="md5-srt")
fake_a._entry("说明.txt", movies_a["FileId"], "file", size=5, etag="md5-txt")
bdmv = fake_a._entry("BDMV", movies_a["FileId"], "dir")
fake_a._entry("index.bdmv", bdmv["FileId"], "file", size=1, etag="md5-bd")
# 盘A：/剧集/第一季/E01.mkv（嵌套目录）
tv_a = fake_a._entry("剧集", 0, "dir")
s1_a = fake_a._entry("第一季", tv_a["FileId"], "dir")
fake_a._entry("E01.mkv", s1_a["FileId"], "file", size=2000, etag="md5-e01")
# 盘B：/电影/流浪地球.mkv
movies_b = fake_b._entry("电影", 0, "dir")
fake_b._entry("流浪地球.mkv", movies_b["FileId"], "file", size=3000, etag="md5-wl")

tmp_root = Path(tempfile.mkdtemp(prefix="p123strm_"))
local_a = tmp_root / "strm_a"
local_b = tmp_root / "strm_b"
local_tv = tmp_root / "strm_tv"
mappings_text = (
    f"{local_a}#/盘A/电影\n{local_b}#/盘B/电影\n{local_tv}#/盘A/剧集"
)

helper = StrmHelper(
    api=api,
    moviepilot_address="http://127.0.0.1:3000",
)

# ============ 1. 路径映射解析与匹配 ============
print("== 1. 路径映射解析与匹配 ==")
mappings = helper.parse_mappings(mappings_text)
check(len(mappings) == 3, f"解析 3 条映射: {mappings}")
check(helper.parse_mappings("") == [], "空配置返回空列表")
check(helper.parse_mappings("#注释\n") == [], "注释行被忽略")
check(helper.parse_mappings("坏行") == [], "无效行被忽略")

local, pan = helper.match_media_path("/盘A/电影/阿凡达.mkv", mappings)
check(local == str(local_a) and pan == "/盘A/电影", "文件路径匹配到盘A电影目录")
local, pan = helper.match_media_path("/盘B/电影/流浪地球.mkv", mappings)
check(local == str(local_b) and pan == "/盘B/电影", "文件路径匹配到盘B电影目录")
local, pan = helper.match_media_path("/盘A/剧集/E01.mkv", mappings)
check(local == str(local_tv) and pan == "/盘A/剧集", "嵌套路径匹配最长前缀")
local, pan = helper.match_media_path("/盘A/纪录片/x.mkv", mappings)
check(local is None, "未匹配的路径返回 None")

check(helper.is_media_file("a.mkv") and helper.is_media_file("A.MKV"), "媒体扩展名识别（含大写）")
check(not helper.is_media_file("a.srt") and not helper.is_media_file(""), "非媒体扩展名识别")

# ============ 2. STRM URL 构建 ============
print("== 2. STRM URL 构建 ==")
url = helper.build_strm_url("阿凡达.mkv", 1000, "md5-av", "s3-av", disk_name="盘A")
check(url is not None and url.startswith("http://127.0.0.1:3000/api/v1/plugin/P123DiskMulti/redirect_url"), "URL 前缀正确")
check("apikey=test-api-token" in url, "URL 含 apikey")
check("s3_key_flag=s3-av" in url and "md5=md5-av" in url and "size=1000" in url, "URL 含文件参数")
check("disk=%E7%9B%98A" in url, "URL 含网盘名参数（URL编码）")
no_addr = StrmHelper(api=api, moviepilot_address="")
check(no_addr.build_strm_url("a.mkv", 1, "m", "s") is None, "未配置地址时返回 None")

# ============ 3. 302 换下载地址 ============
print("== 3. 302 换下载地址 ==")
dl = helper.resolve_download_url("阿凡达.mkv", 1000, "md5-av", "s3-av")
check(dl == "http://download/1", f"带 S3KeyFlag 直接换取: {dl}")
dl = helper.resolve_download_url("阿凡达.mkv", 1000, "md5-av", "s3-av", disk_name="盘B")
check(dl == "http://download/1", "指定其他盘账号也能换取（S3KeyFlag 全局有效）")
dl = helper.resolve_download_url("旧文件.mkv", 2000, "md5-old", "", disk_name="盘A")
check(dl == "http://download/1", "无 S3KeyFlag 时秒传转存后换取")
check(fake_a._has_dir("我的秒传"), "转存目录「我的秒传」已创建")
dl = helper.resolve_download_url("x.mkv", 0, "", "", disk_name="盘A")
check(dl is None, "无标识且无 md5/size 时返回 None")

# ============ 4. 全量同步 ============
print("== 4. 全量同步（多盘） ==")
result = helper.full_sync(mappings_text, overwrite=False)
print(f"  (结果: ok={result['ok']} skip={result['skip']} fail={result['fail']})")
check(result["ok"] == 3, "生成 3 个 STRM（阿凡达+E01+流浪地球）")
av_strm = local_a / "阿凡达.strm"
check(av_strm.exists(), "盘A 电影目录 STRM 生成")
content = av_strm.read_text(encoding="utf-8")
check("redirect_url" in content and "s3_key_flag=s3" in content and "apikey=test-api-token" in content, "STRM 内容为 302 URL")
check(not (local_a / "阿凡达.srt.strm").exists(), "字幕文件不生成 STRM")
check(not (local_a / "说明.txt.strm").exists(), "文本文件不生成 STRM")
check(not (local_a / "BDMV" / "index.bdmv.strm").exists(), "蓝光原盘目录跳过")
e01_strm = local_tv / "第一季" / "E01.strm"
check(e01_strm.exists(), "嵌套目录 STRM 生成（相对路径保留）")
check((local_b / "流浪地球.strm").exists(), "盘B 电影 STRM 生成（多盘支持）")

result2 = helper.full_sync(mappings_text, overwrite=False)
check(result2["ok"] == 0 and result2["skip"] == 3, f"默认模式跳过已存在: ok={result2['ok']} skip={result2['skip']}")
result3 = helper.full_sync(mappings_text, overwrite=True)
check(result3["ok"] == 3 and result3["skip"] == 0, "覆盖模式重新生成")

# ============ 5. 监控整理事件 ============
print("== 5. 监控整理事件 ==")


def make_target_item(path, name, size, md5, flag):
    return FileItem(
        storage="123云盘",
        path=path,
        name=name,
        type="file",
        pickcode=str({
            "FileName": name, "Size": size, "Etag": md5, "S3KeyFlag": flag,
        }),
    )


# 5.1 正常入库
item = make_target_item("/盘A/电影/新电影.mkv", "新电影.mkv", 5000, "md5-new", "s3-new")
strm_path = helper.handle_transfer_complete(item, "/盘A/电影", mappings_text)
check(strm_path is not None and strm_path.name == "新电影.strm", f"入库生成 STRM: {strm_path}")
check("s3_key_flag=s3-new" in strm_path.read_text(encoding="utf-8"), "STRM 内容正确")
# 5.2 蓝光目录跳过
item_bd = make_target_item("/盘A/电影/BDMV/index.bdmv", "index.bdmv", 1, "m", "s")
check(helper.handle_transfer_complete(item_bd, "/盘A/电影/BDMV", mappings_text) is None, "蓝光原盘跳过")
# 5.3 非媒体跳过
item_sub = make_target_item("/盘A/电影/新电影.srt", "新电影.srt", 10, "m2", "s2")
check(helper.handle_transfer_complete(item_sub, "/盘A/电影", mappings_text) is None, "非媒体文件跳过")
# 5.4 路径不匹配跳过
item_out = make_target_item("/盘A/纪录片/E02.mkv", "E02.mkv", 10, "m3", "s3")
check(helper.handle_transfer_complete(item_out, "/盘A/纪录片", mappings_text) is None, "未匹配输出目录跳过")
# 5.5 pickcode 缺失跳过
item_noinfo = FileItem(storage="123云盘", path="/盘A/电影/x.mkv", name="x.mkv", type="file")
check(helper.handle_transfer_complete(item_noinfo, "/盘A/电影", mappings_text) is None, "缺少文件信息跳过")

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
