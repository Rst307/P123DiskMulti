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

# ============ 6. 后台异步全量同步 ============
print("== 6. 后台异步全量同步 ==")
done_flag = {}


def on_done(result):
    done_flag["result"] = result


started = helper.start_full_sync(mappings_text, overwrite=False, on_done=on_done)
check(started is True, "start_full_sync 立即返回 True")
# 运行中再次触发应被拒绝
check(helper.start_full_sync(mappings_text, overwrite=False) is False, "运行中再次启动被拒绝")
busy = helper.full_sync(mappings_text, overwrite=False)
check("已有全量同步任务" in busy.get("errors", [""])[0], "运行中同步调用返回忙碌错误")
# 轮询等待后台完成（最多 15 秒）
import time
waited = 0
while helper.sync_status().get("running") and waited < 150:
    time.sleep(0.1)
    waited += 1
st = helper.sync_status()
check(not st.get("running"), "后台同步最终完成（running=False）")
check(st.get("last_result") is not None, "状态含上次结果")
check(st.get("last_time"), "状态含上次时间")
check(st["last_result"]["ok"] == 0 and st["last_result"]["skip"] == 3, "后台结果正确（默认模式跳过已存在）")
check("result" in done_flag and done_flag["result"]["skip"] == 3, "完成回调被调用")

# ============ 7. 换链域名风控自动切换 ============
print("== 7. 换链域名风控自动切换 ==")
import P123DiskMulti.tool as tool_mod  # noqa: E402

# 新建独立账号环境（避免污染前序测试的域名状态）
api2 = P123MultiApi(disks=[], disk_name="123云盘")
acc_c = tl.make_account("盘C", 10 * 1024 ** 3, 1 * 1024 ** 3)
api2._accounts = [acc_c]
fake_c = acc_c.client._fake
helper2 = StrmHelper(api=api2, moviepilot_address="http://127.0.0.1:3000")

_orig_probe = tool_mod.probe_download_url
probe_queue: list = []


def _fake_probe(url, headers=None, timeout=8):
    return probe_queue.pop(0)


tool_mod.probe_download_url = _fake_probe

# 7.1 第一候选域名正常：直接返回
fake_c.last_download_bases = []
probe_queue = [(True, 206, "")]
dl = helper2.resolve_download_url("a.mkv", 100, "m1", "s1", disk_name="盘C")
check(dl == "http://download/1", "票有效时直接返回")
check(fake_c.last_download_bases == ["https://api.123278.com/b"],
      f"默认优先使用 api.123278.com/b: {fake_c.last_download_bases}")

# 7.2 第一候选被风控（403 50002/1010）→ 自动切换第二域名
fake_c.last_download_bases = []
probe_queue = [
    (False, 403, "message=download err: 50002 code=1010"),
    (True, 206, ""),
]
dl = helper2.resolve_download_url("b.mkv", 100, "m2", "s2", disk_name="盘C")
check(dl == "http://download/1", "被风控后切换域名换链成功")
check(fake_c.last_download_bases == ["https://api.123278.com/b", ""],
      f"先默认域名后自动切换: {fake_c.last_download_bases}")

# 7.3 工作域名记忆：第二次起直接走验证通过的域名
fake_c.last_download_bases = []
probe_queue = [(True, 206, "")]
dl = helper2.resolve_download_url("c.mkv", 100, "m3", "s3", disk_name="盘C")
check(dl == "http://download/1", "再次换链成功")
check(fake_c.last_download_bases == [""],
      f"优先使用已验证域名(默认域): {fake_c.last_download_bases}")

# 7.4 全部通道被风控 → 返回 None
probe_queue = [
    (False, 403, "message=download err: 50002 code=1010"),
    (False, 403, "message=download err: 50002 code=1010"),
]
dl = helper2.resolve_download_url("d.mkv", 100, "m4", "s4", disk_name="盘C")
check(dl is None, "全部通道被风控时返回 None")

# 7.5 探测网络异常（无法判定）→ 按原链接返回，不做切换
probe_queue = [(False, 0, "ConnectionError: x")]
dl = helper2.resolve_download_url("e.mkv", 100, "m5", "s5", disk_name="盘C")
check(dl == "http://download/1", "探测网络异常时按原链接返回")

# 7.6 地域绑定 403（realc/city）→ 记日志并继续尝试其他通道
probe_queue = [
    (False, 403, "message=forbidden realc:chongqing city arg_c:shanghai code=1010"),
    (True, 206, ""),
]
dl = helper2.resolve_download_url("f.mkv", 100, "m6", "s6", disk_name="盘C")
check(dl == "http://download/1", "地域绑定错误也尝试切换通道")

# 7.7 关闭探测：不验证直接返回换链结果
helper3 = StrmHelper(
    api=api2, moviepilot_address="http://127.0.0.1:3000", download_probe=False
)
fake_c.last_download_bases = []
probe_queue = [(False, 403, "message=download err: 50002 code=1010")]  # 不应被消费
fake_c.last_download_bases = []
dl = helper3.resolve_download_url("g.mkv", 100, "m7", "s7", disk_name="盘C")
check(dl == "http://download/1", "关闭探测时直接返回换链结果")
check(len(probe_queue) == 1 and fake_c.last_download_bases == [""],
      "关闭探测时未调用探测、且沿用已验证域名")

# 7.8 自定义换链域名候选
helper4 = StrmHelper(
    api=api2,
    moviepilot_address="http://127.0.0.1:3000",
    download_base_urls=["https://custom.example.com/b"],
)
fake_c.last_download_bases = []
probe_queue = [(True, 206, "")]
dl = helper4.resolve_download_url("h.mkv", 100, "m8", "s8", disk_name="盘C")
check(dl == "http://download/1", "自定义域名换链成功")
check(fake_c.last_download_bases == ["https://custom.example.com/b"],
      f"使用自定义域名: {fake_c.last_download_bases}")

tool_mod.probe_download_url = _orig_probe

# ============ 8. 下载链接缓存（降低签票频率，防风控触发） ============
print("== 8. 下载链接缓存 ==")
api3 = P123MultiApi(disks=[], disk_name="123云盘")
acc_d = tl.make_account("盘D", 10 * 1024 ** 3, 1 * 1024 ** 3)
api3._accounts = [acc_d]
fake_d = acc_d.client._fake
helper5 = StrmHelper(api=api3, moviepilot_address="http://127.0.0.1:3000")

tool_mod.probe_download_url = _fake_probe

# 8.1 首次请求：签票一次
fake_d.last_download_bases = []
probe_queue = [(True, 206, "")]
dl = helper5.resolve_download_url("i.mkv", 100, "m9", "s9", disk_name="盘D")
check(dl == "http://download/1", "首次换链成功")
check(fake_d.last_download_bases == ["https://api.123278.com/b"],
      f"首次签票一次: {fake_d.last_download_bases}")

# 8.2 同一文件 TTL 内再次请求（Emby HEAD+GET）：命中缓存，不再签票
fake_d.last_download_bases = []
probe_queue = [(True, 206, "")]  # 若发生探测会被消费，命中缓存则原样保留
fake_d.last_download_bases = []
dl = helper5.resolve_download_url("i.mkv", 100, "m9", "s9", disk_name="盘D")
check(dl == "http://download/1", "缓存命中返回相同链接")
check(fake_d.last_download_bases == [],
      f"缓存命中未再签票: {fake_d.last_download_bases}")
check(len(probe_queue) == 1, "缓存命中未再探测")

# 8.3 不同文件不受缓存影响
fake_d.last_download_bases = []
probe_queue = [(True, 206, "")]
dl = helper5.resolve_download_url("j.mkv", 100, "m10", "s10", disk_name="盘D")
check(dl == "http://download/1", "其他文件正常签票")
check(len(fake_d.last_download_bases) == 1, "不同文件重新签票")

# 8.4 关闭缓存（download_cache_ttl=0）：每次请求都签票
helper6 = StrmHelper(
    api=api3, moviepilot_address="http://127.0.0.1:3000", download_cache_ttl=0
)
fake_d.last_download_bases = []
probe_queue = [(True, 206, ""), (True, 206, "")]
d1 = helper6.resolve_download_url("k.mkv", 100, "m11", "s11", disk_name="盘D")
d2 = helper6.resolve_download_url("k.mkv", 100, "m11", "s11", disk_name="盘D")
check(d1 == "http://download/1" and d2 == "http://download/1", "关闭缓存仍可换链")
check(len(fake_d.last_download_bases) == 2,
      f"关闭缓存时每次请求都签票: {fake_d.last_download_bases}")

tool_mod.probe_download_url = _orig_probe

# ============ 9. 分享票播放模式 ============
print("== 9. 分享票播放模式 ==")
import time as _time  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from P123DiskMulti.share import ShareSync  # noqa: E402
from P123DiskMulti.share_ticket import ticket_ttl  # noqa: E402

share_tmp = tempfile.mkdtemp(prefix="p123share_")
api4 = P123MultiApi(disks=[], disk_name="123云盘")
acc_e = make_account("盘E", 100 * 1024 ** 3, 50 * 1024 ** 3)
api4._accounts = [acc_e]
fake_e = acc_e.client._fake

share_task = ShareSync(
    api=api4,
    task_id="T1",
    name="分享1",
    share_key="u9izjv-WeSWv",
    share_pwd="Zee5",
    target_vpath="/盘E/分享",
    db_path=_Path(share_tmp) / "share.sqlite3",
)
# 转存记录（带分享侧 FileId/S3KeyFlag）
share_task._db.add(
    file_key="k1", task_id="T1", name="S01E240.2025.2160p.mp4",
    rel_path="/电视剧/完美世界/S01E240.mp4", size=502475797,
    etag="md5-share1", share_fp="fp1",
    target_path="/盘E/分享/电视剧/完美世界/S01E240.mp4",
    file_id="40123967", share_s3_key_flag="1830563249-0",
)
# 第二记录（分享失效测试用）
share_task._db.add(
    file_key="k2", task_id="T1", name="第二集.mkv",
    rel_path="/电视剧/完美世界/第二集.mkv", size=300,
    etag="md5-share2", share_fp="fp1",
    target_path="/盘E/分享/电视剧/完美世界/第二集.mkv",
    file_id="40123968", share_s3_key_flag="1830563249-0",
)
# 老记录（无 file_id，实时定位回填测试用）
share_task._db.add(
    file_key="k3", task_id="T1", name="老片.mkv",
    rel_path="/电影/老片.mkv", size=200, etag="md5-old",
    share_fp="fp1", target_path="/盘E/分享/电影/老片.mkv",
)

helper7 = StrmHelper(
    api=api4, moviepilot_address="http://127.0.0.1:3000",
    ticket_mode="share", shares=[share_task],
)
helper8 = StrmHelper(
    api=api4, moviepilot_address="http://127.0.0.1:3000",
    ticket_mode="auto", shares=[share_task],
)

# 9.1 分享票换票 + 210 解析
_PAYLOAD = {
    "shareKey": "u9izjv-WeSWv",
    "sharePwd": "Zee5",
    "fileId": 40123967,
    "s3KeyFlag": "1830563249-0",
    "etag": "md5-share1",
    "size": 502475797,
}
tl._share_download_calls.clear()
dl = helper7.resolve_download_url(
    "S01E240.2025.2160p.mp4", 502475797, "md5-share1", "", disk_name="盘E"
)
check(dl == "http://edge.example/file", "分享票 210 解析返回边缘 URL")
check(
    len(tl._share_download_calls) == 1
    and tl._share_download_calls[0]["json"] == _PAYLOAD,
    f"换票载荷正确: {tl._share_download_calls[0]['json'] if tl._share_download_calls else None}",
)

# 9.2 票缓存：二次播放不再换票（仍做 210 解析）
tl._share_download_calls.clear()
dl = helper7.resolve_download_url(
    "S01E240.2025.2160p.mp4", 502475797, "md5-share1", "", disk_name="盘E"
)
check(dl == "http://edge.example/file", "缓存命中仍返回边缘 URL")
check(len(tl._share_download_calls) == 0, "缓存命中未再换票")

# 9.3 分享票模式：无分享记录 → None
_orig_probe2 = tool_mod.probe_download_url
probe_queue = []  # 不应触发 VIP 换票
probe_queue = [(True, 206, "")]
dl = helper7.resolve_download_url("别的.mkv", 100, "md5-none", "s3x", disk_name="盘E")
check(dl is None, "分享票模式无分享记录返回 None")
check(len(probe_queue) == 1, "未走 VIP 通道")
tool_mod.probe_download_url = _orig_probe2

# 9.4 自动模式：无分享记录 → 回退 VIP 直链
dl = helper8.resolve_download_url("别的.mkv", 100, "md5-none", "s3x", disk_name="盘E")
check(dl == "http://download/1", "auto 模式无分享记录回退 VIP 直链")

# 9.5 分享失效（code=400）→ None，日志可辨识
tl.fake_post._response = {"code": 400, "message": "非法请求，源文件不存在"}
dl = helper7.resolve_download_url(
    "第二集.mkv", 300, "md5-share2", "", disk_name="盘E"
)
check(dl is None, "分享失效(code=400)返回 None")
tl.fake_post._response = None

# 9.6 需登录（code=5112）→ None
tl.fake_post._response = {"code": 5112, "message": "您需要注册登录或付费后下载"}
dl = helper7.resolve_download_url(
    "第二集.mkv", 300, "md5-share2", "", disk_name="盘E"
)
check(dl is None, "需登录(code=5112)返回 None")
tl.fake_post._response = None

# 9.7 老记录无 file_id：按 rel_path 实时定位并回填
fake_e.share_entries = {
    0: [{"FileId": 55, "FileName": "电影", "Type": 1, "Size": 0,
         "Etag": "", "S3KeyFlag": "", "ParentFileId": 0}],
    55: [{"FileId": 777, "FileName": "老片.mkv", "Type": 0, "Size": 200,
          "Etag": "md5-old", "S3KeyFlag": "111-0", "ParentFileId": 55}],
}
tl._share_download_calls.clear()
dl = helper7.resolve_download_url("老片.mkv", 200, "md5-old", "", disk_name="盘E")
check(dl == "http://edge.example/file", "老记录实时定位后换票成功")
recs = share_task._db.find_by_etag("md5-old", 200)
check(
    recs and recs[0]["file_id"] == "777"
    and recs[0]["share_s3_key_flag"] == "111-0",
    "FileId/S3KeyFlag 已回填",
)
check(
    tl._share_download_calls
    and tl._share_download_calls[-1]["json"]["fileId"] == 777,
    "换票使用定位到的 FileId",
)

# 9.8 分享通道风控（210 解析 403）：自动换票重试；解除后自动恢复
tl.fake_get._share_210_403 = True
tl._share_download_calls.clear()
dl = helper7.resolve_download_url("老片.mkv", 200, "md5-old", "", disk_name="盘E")
check(dl is None, "分享通道风控返回 None")
tl.fake_get._share_210_403 = False
dl = helper7.resolve_download_url("老片.mkv", 200, "md5-old", "", disk_name="盘E")
check(dl == "http://edge.example/file", "风控解除后自动恢复")
check(len(tl._share_download_calls) == 2, "风控期换新票 + 恢复后重新换票")

# 9.9 票有效期（t 参数）换算
check(
    600 < ticket_ttl("http://x/?v=5&t=9999999999") <= 6 * 3600,
    f"t 有效期换算正确: {ticket_ttl('http://x/?v=5&t=9999999999')}",
)
check(ticket_ttl("http://x/?v=5") == 600, "无 t 参数用默认 600")
check(
    ticket_ttl(f"http://x/?v=5&t={int(_time.time()) + 60}") == 0,
    "t 余量不足 5 分钟不缓存",
)

# ============ 10. 自分享目录服务（独立服务：自己分享文件走分享票） ============
print("== 10. 自分享目录服务 ==")
from P123DiskMulti.self_share import (  # noqa: E402
    SELF_SHARE_API_BASE,
    SelfShareManager,
)

self_share_tmp = tempfile.mkdtemp(prefix="p123selfshare_")
api5 = P123MultiApi(disks=[], disk_name="123云盘")
acc_f = make_account("盘F", 100 * 1024 ** 3, 10 * 1024 ** 3)
api5._accounts = [acc_f]
fake_f = acc_f.client._fake

# 网盘内目录树：/盘F/电影/S01.mkv（自分享目录 = /盘F/电影）
fake_f._entry("电影", 0, "dir")
film_dir = [
    e for e in fake_f.entries.values()
    if e["FileName"] == "电影" and e["ParentFileId"] == 0
][0]
fake_f._entry("S01.mkv", film_dir["FileId"], "file", size=502475797, etag="md5-self1")
# 分享内文件树（建分享后遍历用）：根目录直接列出分享内容
fake_f.share_entries = {
    0: [
        {
            "FileId": 901, "FileName": "S01.mkv", "Type": 0,
            "Size": 502475797, "Etag": "md5-self1",
            "S3KeyFlag": "111-0", "ParentFileId": 0,
        }
    ]
}

ssm = SelfShareManager(
    api=api5,
    db_path=_Path(self_share_tmp) / "selfshare.sqlite3",
)
ssm.set_dirs([("/盘F/电影", "")])

# 10.1 首次同步：建分享 + 索引
r = ssm.sync_dir("/盘F/电影", "")
check(r.get("ok") and r.get("files") == 1, f"首次同步建分享+索引: {r}")
check(len(fake_f.share_create_calls) == 1, "调用 share_create 一次")
check(
    fake_f.share_create_calls[0]["payload"]["fileIdList"] == str(film_dir["FileId"]),
    "分享的是网盘目录 FileId",
)
check(
    fake_f.share_create_calls[0]["base_url"] == SELF_SHARE_API_BASE,
    "建分享走 api.123278.com/b 通道",
)

# 10.2 再次同步：复用已有分享（不重复建）
fake_f.share_create_calls.clear()
r = ssm.sync_dir("/盘F/电影", "")
check(r.get("ok"), "重复同步成功")
check(len(fake_f.share_create_calls) == 0, "已有分享直接复用")

# 10.3 播放：分享票模式走自分享记录（独立于分享增量同步）
helper10 = StrmHelper(
    api=api5, moviepilot_address="http://127.0.0.1:3000",
    ticket_mode="share", shares=[], self_share=ssm,
)
tl._share_download_calls.clear()
dl = helper10.resolve_download_url(
    "S01.mkv", 502475797, "md5-self1", "", disk_name="盘F"
)
check(dl == "http://edge.example/file", "自分享记录分享票播放成功")
check(
    len(tl._share_download_calls) == 1
    and tl._share_download_calls[0]["json"]["shareKey"] == fake_f.shares[-1]["ShareKey"],
    f"换票使用自建分享的 shareKey: {tl._share_download_calls[0]['json']['shareKey'] if tl._share_download_calls else None}",
)

# 10.4 分享失效自动重建（遍历失败 + 分享列表无此分享）
fake_f.shares.clear()  # 分享被取消
fake_f.share_create_calls.clear()
fake_f.fail_share_fs_list_once = True
r = ssm.sync_dir("/盘F/电影", "")
check(r.get("ok"), "分享失效后自动重建成功")
check(len(fake_f.share_create_calls) == 1, "失效后重新建分享")
new_key = fake_f.shares[-1]["ShareKey"]
recs = ssm.find_by_etag("md5-self1", 502475797)
check(
    recs and recs[0]["share_key"] == new_key,
    f"索引已更新为新分享: {recs[0]['share_key'] if recs else None}",
)

# 10.5 手动重建：取消旧分享 + 重新建分享 + 重新索引
fake_f.share_cancel_calls.clear()
fake_f.share_create_calls.clear()
targets = ssm.rebuild("/盘F/电影")
check(targets == ["/盘F/电影"], f"rebuild 返回目标目录: {targets}")
ssm.sync_dir("/盘F/电影", "")
check(len(fake_f.share_cancel_calls) == 1, "重建时取消旧分享")
check(len(fake_f.share_create_calls) == 1, "重建后重新建分享")

# 10.6 分享内文件移除后自动清理
fake_f.share_entries = {0: []}
ssm.sync_dir("/盘F/电影", "")
check(
    ssm.find_by_etag("md5-self1", 502475797) == [],
    "分享内已移除的文件索引被清理",
)

# 10.7 独立服务：未配置分享增量同步时播放回退 VIP（auto 模式）
helper11 = StrmHelper(
    api=api5, moviepilot_address="http://127.0.0.1:3000",
    ticket_mode="auto", shares=[], self_share=ssm,
)
dl = helper11.resolve_download_url(
    "别的.mkv", 100, "md5-none", "s3x", disk_name="盘F"
)
check(dl == "http://download/1", "无自分享记录时 auto 回退 VIP 直链")

# ============ 11. 按需分享模式（播放时懒建带有效期分享） ============
print("== 11. 按需分享（懒建分享） ==")
from P123DiskMulti.share_ticket import (  # noqa: E402
    ON_DEMAND_API_BASE,
    OnDemandShareCache,
    get_on_demand_share_url,
)

api6 = P123MultiApi(disks=[], disk_name="123云盘")
acc_g = make_account("盘G", 100 * 1024 ** 3, 10 * 1024 ** 3)
api6._accounts = [acc_g]
fake_g = acc_g.client._fake

# 网盘内已存在的文件（直接上传/转存均可，无需任何分享记录）
fake_g._entry("MOV.A.mkv", 0, "file", size=999, etag="md5-ond")
fake_g._entry("SUB.srt", 0, "file", size=123, etag="md5-sub")

# 11.1 首次播放：秒传定位 -> 建分享（带有效期）-> 换票 -> 边缘 URL
helper12 = StrmHelper(
    api=api6, moviepilot_address="http://127.0.0.1:3000",
    ticket_mode="on_demand",
)
fake_g.upload_request_calls.clear()
fake_g.share_create_calls.clear()
tl._share_download_calls.clear()
dl = helper12.resolve_download_url("MOV.A.mkv", 999, "md5-ond", "", disk_name="盘G")
check(dl == "http://edge.example/file", f"按需分享首次播放成功: {dl}")
check(
    fake_g.upload_request_calls
    and fake_g.upload_request_calls[0]["payload"]["etag"] == "md5-ond"
    and fake_g.upload_request_calls[0]["payload"]["size"] == 999
    and fake_g.upload_request_calls[0]["base_url"] == ON_DEMAND_API_BASE,
    "秒传定位：md5+size 且走 api.123278.com/b 通道",
)
check(len(fake_g.share_create_calls) == 1, "自动建分享一次")
loc_id = [
    e["FileId"] for e in fake_g.entries.values()
    if e["FileName"] == "MOV.A.mkv"
][0]
sp = fake_g.share_create_calls[0]["payload"]
check(
    sp["fileIdList"] == str(loc_id) and sp["sharePwd"] == ""
    and sp["fillPwdSwitch"] == 0
    and fake_g.share_create_calls[0]["base_url"] == ON_DEMAND_API_BASE,
    f"建分享参数：fileIdList={sp.get('fileIdList')} 免提取码 走 api.123278.com/b",
)
import datetime as _dt
exp = _dt.datetime.strptime(sp["expiration"], "%Y-%m-%dT%H:%M:%S+08:00")
now_dt = _dt.datetime.now()
check(
    (exp - now_dt) > _dt.timedelta(days=6)
    and (exp - now_dt) < _dt.timedelta(days=8),
    f"有效期默认 7 天: {sp['expiration']}",
)
check(
    tl._share_download_calls
    and tl._share_download_calls[0]["json"]["shareKey"] == fake_g.shares[-1]["ShareKey"],
    "换票使用自动创建的分享",
)

# 11.2 有效期内再次播放：不重新建分享（缓存复用）
fake_g.share_create_calls.clear()
dl = helper12.resolve_download_url("MOV.A.mkv", 999, "md5-ond", "", disk_name="盘G")
check(dl == "http://edge.example/file", "有效期内二次播放成功")
check(len(fake_g.share_create_calls) == 0, "有效期内复用分享，不重复建分享")

# 11.3 分享到期：自动取消旧分享并重建
key_od = ("盘G", "md5-ond", 999)
od_rec = helper12._on_demand_share_cache.take(key_od)
od_rec["expired_at"] = time.time() - 1  # 模拟已到期
helper12._on_demand_share_cache.set(key_od, od_rec)
fake_g.share_cancel_calls.clear()
fake_g.share_create_calls.clear()
old_id = od_rec["share_id"]
dl = helper12.resolve_download_url("MOV.A.mkv", 999, "md5-ond", "", disk_name="盘G")
check(dl == "http://edge.example/file", "到期后重建分享播放成功")
check(
    len(fake_g.share_cancel_calls) == 1
    and str(fake_g.share_cancel_calls[0]["payload"]) == str(old_id),
    f"到期自动取消旧分享: {fake_g.share_cancel_calls}",
)
check(len(fake_g.share_create_calls) == 1, "到期后重新建分享")

# 11.4 文件不在网盘（定位失败）：返回 None，不建分享
fake_g.share_create_calls.clear()
dl = helper12.resolve_download_url("GHOST.mkv", 12345, "md5-ghost", "", disk_name="盘G")
check(dl is None, "定位失败返回 None")
check(len(fake_g.share_create_calls) == 0, "定位失败不建分享")

# 11.5 建分享接口失败：返回 None（先清缓存确保走到建分享路径）
helper12._on_demand_share_cache.take(key_od)
fake_g.fail_share_create = True
fake_g.share_create_calls.clear()
dl = helper12.resolve_download_url("MOV.A.mkv", 999, "md5-ond", "", disk_name="盘G")
check(dl is None, "建分享失败返回 None")
fake_g.fail_share_create = False

# 11.6 分享票通道 403 风控：缓存复用失败 -> 取消旧分享重建一次，恢复后自愈
fake_g.share_create_calls.clear()
fake_g.share_cancel_calls.clear()
helper12._on_demand_share_cache.take(key_od)  # 用 11.3 留下的分享缓存
helper12._on_demand_share_cache.set(key_od, od_rec)  # 重新放入（未过期）
tl.fake_get._share_210_403 = True
dl = helper12.resolve_download_url("MOV.A.mkv", 999, "md5-ond", "", disk_name="盘G")
check(dl is None, "通道风控时返回 None")
check(
    len(fake_g.share_create_calls) == 1 and len(fake_g.share_cancel_calls) == 1,
    f"风控时取消旧分享并重建一次: create={len(fake_g.share_create_calls)} cancel={len(fake_g.share_cancel_calls)}",
)
tl.fake_get._share_210_403 = False
dl = helper12.resolve_download_url("MOV.A.mkv", 999, "md5-ond", "", disk_name="盘G")
check(dl == "http://edge.example/file", "风控恢复后播放自愈")

# 11.7 自定义有效期（1 天）+ 提取码
helper13 = StrmHelper(
    api=api6, moviepilot_address="http://127.0.0.1:3000",
    ticket_mode="on_demand",
    on_demand_share_days=1,
    on_demand_share_pwd="pwd123",
)
fake_g.share_create_calls.clear()
dl = helper13.resolve_download_url("SUB.srt", 123, "md5-sub", "", disk_name="盘G")
check(dl == "http://edge.example/file", "自定义参数播放成功")
sp2 = fake_g.share_create_calls[0]["payload"]
exp2 = _dt.datetime.strptime(sp2["expiration"], "%Y-%m-%dT%H:%M:%S+08:00")
check(
    (exp2 - now_dt) > _dt.timedelta(hours=20)
    and (exp2 - now_dt) < _dt.timedelta(days=2),
    f"自定义有效期 1 天: {sp2['expiration']}",
)
check(
    sp2["sharePwd"] == "pwd123" and sp2["fillPwdSwitch"] == 1,
    "自定义提取码写入分享",
)

# 11.8 独立函数冒烟：OnDemandShareCache 上限与逐出
odc = OnDemandShareCache(maxsize=2)
odc.set(("a", "m1", 1), {"share_id": "s1", "expired_at": time.time() + 10})
odc.set(("a", "m2", 2), {"share_id": "s2", "expired_at": time.time() + 20})
odc.set(("a", "m3", 3), {"share_id": "s3", "expired_at": time.time() + 30})
check(odc.size() == 2, "缓存超过上限自动逐出")
check(odc.take(("a", "m1", 1)) is None, "最早过期条目被逐出")

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
