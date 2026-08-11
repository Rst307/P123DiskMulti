# AGENTS.md — P123DiskMulti 项目指南

MoviePilot 插件 **123云盘多盘合并**：将多个 123 网盘账号合并为一个存储空间，支持网盘间互传、空间不足自动切换网盘、STRM 播放（Emby）、分享增量同步、自分享目录、定期目录整理。

作者 Rst307，基于 DDSRem 的 [P123Disk](https://github.com/DDSRem-Dev/MoviePilot-Plugins/tree/main/plugins.v2/p123disk) 扩展开发。

## 目录结构

```
package.v2.json                    # 插件清单（版本权威来源 + history 变更记录）
plugins.v2/p123diskmulti/
  __init__.py                      # 插件入口：init_plugin、定时任务、/redirect_url 播放 API
  tool.py                          # P123AutoClient（代理层）、TokenStore、下载换链域名状态机
  p123_api.py                      # P123MultiApi（多盘聚合）、DiskAccount、上传/互传/快照
  share_ticket.py                  # 分享票换票（vip/share/auto/on_demand 模式）、5112 自愈
  strm.py                          # STRM 生成/同步/播放辅助
  share.py                         # 分享增量同步（SQLite 增量去重）
  self_share.py                    # 自分享目录服务（api.123278.com 通道，永久分享）
  organize.py                      # 定期目录整理（提交 MoviePilot 整理队列）
tests/
  test_logic.py                    # 核心逻辑测试（38 项）
  test_strm.py                     # STRM/分享票测试（152 项）
  test_login.py                    # 登录态持久化测试（23 项）
learn/                             # 学习参考（勿改）
```

## 开发命令

```bash
python tests/test_logic.py     # 直接运行脚本（非 pytest），内置 __main__ 保护
python tests/test_strm.py
python tests/test_login.py
python -c "import ast; ast.parse(open('plugins.v2/p123diskmulti/tool.py', encoding='utf-8').read())"  # 语法检查
```

改完代码必须跑全部三个测试文件，回归基线：**38 + 152 + 23 全部通过**。

## 核心架构

### P123AutoClient（tool.py）— 唯一的客户端入口

- **懒实例化 + 线程安全**：`_ensure_client()` 双检锁保证同一账号全局唯一 `P123Client`，并发操作不产生并发登录
- **登录态持久化（v1.4.11 起）**：`TokenStore` 把 token 写入插件数据目录 `p123tokens.json`（按 passport 隔离，原子写入）；构造客户端优先 `P123Client(passport, password, token=saved, check_for_relogin=False)` —— **带 token 构造不触发登录**（123 token 有效期 30 天）
- **必须使用 `check_for_relogin=False`**：库内"任何 401 都自动重登"是历史登录风暴的根源，禁止重新启用
- **`__getattr__` 代理**：非可调用属性（如 `token`）直接返回值；可调用返回 wrapped（含 401 自愈）。**不要改回"一律返回 wrapped"** —— 会导致 `getattr(client, "token")` 拿到函数而非字符串

### 401 处理约定（两条路径都要覆盖）

| 场景 | 处理 |
|---|---|
| dict 响应 `code==401` 且 message 含 `exceeded the limit`（token 超限） | **不重登**（重登只会产生更多 token），记日志原样返回 |
| dict 响应其他 401 / 抛 `P123AuthenticationError` | `_relogin()`（60 秒冷却合并）后重试一次 |
| 分享票连续 5112（share_ticket `_force_relogin`） | 显式 `relogin()`，**不受冷却限制** |

每次成功登录必须持久化新 token（`_save_token`）。

### 下载换链域名状态机（tool.py）

- 通道被风控（50002/1010/403）→ 探测 + 自动切换换链域名；已验证域名按账号记忆 30 分钟
- 默认优先网页通道 `https://api.123278.com/b`；候选可配置
- 下载直链缓存默认 600 秒（Emby HEAD+GET 各签一次票，命中即不再签票）

### 分享票（share_ticket.py）

- 换票始终以客户端**当前**登录态为准，禁止入口处捕获旧 token 后跨重登使用
- 5112 时依次尝试 open_platform / android 平台模板，均失败才 `_force_relogin` 重试一次
- 认证头必须用小写 `authorization` 键（大写 Authorization 会覆盖 p123client 自带头，导致 401）
- 按需分享（on_demand）：定位用 `file/list/new` 搜索 + `fs_list` 遍历兜底，**禁止用 upload_request 秒传定位**（会在根目录生成重复文件）；**`fs_list` 遍历分页必须用 `next` 游标**（123 服务端忽略 `Page` 参数，next 固定 0 会永远重复第一页，目录超 100 项即定位不到）

## 版本发布约定

1. `package.v2.json` 的 `version` 是权威版本号，更新时**必须**同时：
   - 改 `plugins.v2/p123diskmulti/__init__.py` 的 `plugin_version`（「关于」面板显示它，历史上多次忘记同步）
   - 在 `history` 顶部新增对应版本条目（简述改动，参照现有格式）
   - 必要时在 `README.md` 常见问题补充说明
2. git commit 风格：`v1.4.x: 描述`（正式版）/ `fix: 描述`（中间提交）

## 代码约定

- 注释、日志、文档全中文；日志统一 `【123多盘】` 前缀
- 用 `logger.warn`（项目沿用，不要改成 warning）
- 对外签名改动保持向后兼容：新参数带默认值（如 `DiskAccount(..., token_store=None)`）
- 涉及并发一律加锁（`threading.Lock`），`P123AutoClient` 的锁在 `__init__` 中创建
- 123 接口可能返回 dict 错误也可能抛 `P123AuthenticationError`，处理登录态时两条路径都要覆盖
- 不加新依赖；p123client 是唯一外部库（0.0.9，作者 ChenyangGao），`pip show p123client` 在本环境不可用，查阅源码用仓库克隆或读 p123client 文档

## 测试约定

- 测试**不依赖 MoviePilot 环境**：`test_logic.py` 在导入前把 `sys.modules["p123client"]` 换成 stub（`P123Client = object`、`P123AuthenticationError` 桩），并设置 `P123DiskMulti.__path__` 指向 `plugins.v2/p123diskmulti`
- 新测试文件以 `import test_logic as tl` 开头复用该环境（`test_login.py` 的模式），再 `import P123DiskMulti.tool as _tool` 等
- `make_account` 会替换 `DiskAccount.client` 为 `FakeAutoClient`；涉及 `P123AutoClient` 自身的测试用 `_tool.P123Client = FakeP123` 猴子替换并 `finally` 恢复
- 不要删除 `test_logic.py` 中 `assert _tool.P123Client is object` 的回归守卫（验证 tool.py 保持从 p123client 顶层导入 P123Client）
- 修 bug 先写复现测试（红），再修代码（绿）
