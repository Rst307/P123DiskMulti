"""
P123DiskMulti 登录管理独立测试（不依赖 MoviePilot 环境）

验证登录态持久化与最小化登录策略：
1. TokenStore：token 跨实例持久化 / 损坏文件容错 / 原子写入
2. 复用已保存 token 构造客户端不触发登录（123 token 有效期 30 天）
3. 无 token 时首次登录并自动持久化
4. 并发懒加载只创建一个客户端（不产生并发登录）
5. 「token 数量超限」401 不重登（重登只会产生更多 token）
6. token 失效 401 → 合并式重登一次并重试成功，新 token 持久化
7. 自动重登冷却：短时间重复 401 不反复登录
8. relogin() 显式重登不受冷却限制，且持久化新 token
9. P123AuthenticationError 异常路径自愈（重登一次重试）

运行：python tests/test_login.py
"""
import sys
import tempfile
import threading
from pathlib import Path

# 复用 test_logic 的模拟环境（含 __main__ 保护，导入不会执行测试）
sys.path.insert(0, str(Path(__file__).parent))
import test_logic as tl  # noqa: E402

# 通过 test_logic 注册的包路径导入 tool 模块
import P123DiskMulti.tool as _tool  # noqa: E402

TokenStore = _tool.TokenStore
P123AutoClient = _tool.P123AutoClient

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


class FakeP123:
    """模拟 p123client.P123Client：无 token 构造 = 一次登录；响应可编程"""
    instances = []
    queue = []
    login_count = 0

    def __init__(self, passport="", password="", token=None,
                 check_for_relogin=True):
        self.passport = passport
        self.password = password
        self.check_for_relogin = check_for_relogin
        FakeP123.instances.append(self)
        if token:
            self.token = token
        else:
            FakeP123.login_count += 1
            self.token = f"fake-token-{FakeP123.login_count}"

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args, **kwargs):
            resp = FakeP123.queue.pop(0) if FakeP123.queue else {"code": 0}
            if isinstance(resp, Exception):
                raise resp
            return resp

        return _call


def reset():
    FakeP123.instances = []
    FakeP123.queue = []
    FakeP123.login_count = 0


# ============ 1. TokenStore ============
print("== 1. TokenStore 持久化 ==")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "tokens.json"
    ts1 = TokenStore(p)
    ts1.set("13800000000", "abc.def.ghi")
    ts2 = TokenStore(p)  # 模拟进程重启
    check(ts2.get("13800000000") == "abc.def.ghi", "token 跨实例持久化")
    check(ts2.get("13900000000") == "", "未保存账号返回空串")
    check(not (Path(d) / "tokens.json.tmp").exists(), "原子写入无残留临时文件")
    bad = Path(d) / "bad.json"
    bad.write_text("{{{", "utf-8")
    ts3 = TokenStore(bad)
    check(ts3.get("1") == "", "损坏的 token 文件安全降级为空")

# ============ 2. 复用已保存 token 不登录 ============
print("== 2. 复用已保存 token 不登录 ==")
reset()
orig = _tool.P123Client
_tool.P123Client = FakeP123
try:
    with tempfile.TemporaryDirectory() as d:
        store = TokenStore(Path(d) / "tokens.json")
        store.set("13800000000", "saved-token")
        c = P123AutoClient("13800000000", "pw", token_store=store)
        tok = c.token
        check(tok == "saved-token", "直接复用已保存 token，无登录")
        check(FakeP123.login_count == 0, "复用 token 时登录次数为 0")
        check(
            FakeP123.instances[0].check_for_relogin is False,
            "已关闭库内 401 自动重登（check_for_relogin=False）",
        )
finally:
    _tool.P123Client = orig

# ============ 3. 无 token 首次登录并持久化 ============
print("== 3. 无 token 首次登录并持久化 ==")
reset()
_tool.P123Client = FakeP123
try:
    with tempfile.TemporaryDirectory() as d:
        store = TokenStore(Path(d) / "tokens.json")
        c = P123AutoClient("13800000000", "pw", token_store=store)
        tok = c.token
        check(FakeP123.login_count == 1, "首次访问触发一次登录")
        check(store.get("13800000000") == tok, "登录后 token 已持久化")
finally:
    _tool.P123Client = orig

# ============ 4. 并发懒加载只创建一个客户端 ============
print("== 4. 并发懒加载只创建一个客户端 ==")
reset()
_tool.P123Client = FakeP123
try:
    with tempfile.TemporaryDirectory() as d:
        store = TokenStore(Path(d) / "tokens.json")
        store.set("13800000000", "saved-token")
        c = P123AutoClient("13800000000", "pw", token_store=store)
        results = []

        def worker():
            results.append(c.token)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check(
            len(FakeP123.instances) == 1,
            f"并发 20 线程只创建 1 个客户端（实际 {len(FakeP123.instances)}）",
        )
        check(
            all(r == "saved-token" for r in results),
            "并发访问结果一致（均为复用 token）",
        )
finally:
    _tool.P123Client = orig

# ============ 5. token 超限 401 不重登 ============
print("== 5. token 数量超限 401 不重登 ==")
reset()
_tool.P123Client = FakeP123
try:
    with tempfile.TemporaryDirectory() as d:
        store = TokenStore(Path(d) / "tokens.json")
        store.set("13800000000", "saved-token")
        c = P123AutoClient("13800000000", "pw", token_store=store)
        FakeP123.queue.append(
            {"code": 401, "message": "tokens number has exceeded the limit"}
        )
        r = c.fs_list({})
        check(r.get("code") == 401, "超限 401 原样返回（不伪装成功）")
        check(
            FakeP123.login_count == 0,
            "超限 401 不触发重登（重登只会产生更多 token）",
        )
finally:
    _tool.P123Client = orig

# ============ 6. token 失效 401 → 重登一次重试 ============
print("== 6. token 失效 401 → 重登一次重试成功 ==")
reset()
_tool.P123Client = FakeP123
try:
    with tempfile.TemporaryDirectory() as d:
        store = TokenStore(Path(d) / "tokens.json")
        store.set("13800000000", "old-token")
        c = P123AutoClient("13800000000", "pw", token_store=store)
        FakeP123.queue.append({"code": 401, "message": "token invalid"})
        FakeP123.queue.append({"code": 0, "data": {"ok": True}})
        r = c.fs_list({})
        check(r.get("data", {}).get("ok") is True, "401 后重登重试成功")
        check(FakeP123.login_count == 1, "仅重登一次")
        new_tok = store.get("13800000000")
        check(bool(new_tok) and new_tok != "old-token", "新 token 已持久化")
finally:
    _tool.P123Client = orig

# ============ 7. 自动重登冷却 ============
print("== 7. 自动重登冷却：短时间重复 401 不反复登录 ==")
reset()
_tool.P123Client = FakeP123
try:
    with tempfile.TemporaryDirectory() as d:
        store = TokenStore(Path(d) / "tokens.json")
        store.set("13800000000", "old-token")
        c = P123AutoClient("13800000000", "pw", token_store=store)
        FakeP123.queue.append({"code": 401, "message": "token invalid"})
        FakeP123.queue.append({"code": 0, "data": 1})
        c.fs_list({})  # 触发重登（进入冷却）
        n1 = FakeP123.login_count
        check(n1 == 1, "首次失效触发一次重登")
        FakeP123.queue.append({"code": 401, "message": "token invalid"})
        FakeP123.queue.append({"code": 0, "data": 2})
        r = c.fs_list({})  # 冷却期内再次 401
        check(FakeP123.login_count == n1, "冷却期内不重复登录")
        check(r.get("data") == 2, "冷却期内用现有 token 直接重试")
finally:
    _tool.P123Client = orig

# ============ 8. relogin() 显式重登 ============
print("== 8. relogin() 显式重登不受冷却限制并持久化 ==")
reset()
_tool.P123Client = FakeP123
try:
    with tempfile.TemporaryDirectory() as d:
        store = TokenStore(Path(d) / "tokens.json")
        store.set("13800000000", "old-token")
        c = P123AutoClient("13800000000", "pw", token_store=store)
        FakeP123.queue.append({"code": 401, "message": "token invalid"})
        FakeP123.queue.append({"code": 0, "data": 1})
        c.fs_list({})  # 先进入冷却
        n1 = FakeP123.login_count
        tok = c.relogin()
        check(FakeP123.login_count == n1 + 1, "显式重登不受冷却限制")
        check(store.get("13800000000") == tok, "显式重登后 token 已持久化")
finally:
    _tool.P123Client = orig

# ============ 9. 认证异常路径自愈 ============
print("== 9. P123AuthenticationError 异常路径自愈 ==")
reset()
_tool.P123Client = FakeP123
try:
    with tempfile.TemporaryDirectory() as d:
        store = TokenStore(Path(d) / "tokens.json")
        store.set("13800000000", "old-token")
        c = P123AutoClient("13800000000", "pw", token_store=store)
        FakeP123.queue.append(_tool.P123AuthenticationError("auth failed"))
        FakeP123.queue.append({"code": 0, "data": 3})
        r = c.fs_list({})
        check(r.get("data") == 3, "认证异常重登后重试成功")
        check(FakeP123.login_count == 1, "异常路径仅重登一次")
finally:
    _tool.P123Client = orig

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
