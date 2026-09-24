# 光鸭 STRM（GuangYaStrm）

MoviePilot V3 的“光鸭云盘助手”配套插件。

## 它解决什么问题

光鸭云盘助手把 `/emby/` 作为远程存储暴露给 MoviePilot，但它不是 Linux 文件系统挂载，因此传统 CloudStrm 需要的 `/gy` 之类目录并不存在。

本插件直接调用已经运行的 `ShukGuangYaDisk`：

```text
光鸭云盘 /emby/
       ↓ browse_path(recursion=True)
GuangYaStrm
       ↓
/opt/emby/strm/光鸭云盘/**/*.strm
       ↓
Emby
       ↓ HTTP + Range
GuangYaStrm /play
       ↓
ShukGuangYaDisk.stream_file()
       ↓
光鸭签名下载地址
```

**不会把电影/剧集文件下载到本地。** 本地只产生很小的 `.strm` 文件和一个索引 JSON。

## 前置条件

- MoviePilot V3。
- 已安装并启用“光鸭云盘助手”，插件 ID 默认为 `ShukGuangYaDisk`。
- 光鸭云盘助手已经登录，并且 `/emby/` 能在 MoviePilot 的光鸭存储中正常浏览。
- MoviePilot 容器可以写入 STRM 输出目录。
- Emby 可以访问你填写的 MoviePilot 地址。

## 推荐配置

```text
光鸭插件 ID：ShukGuangYaDisk
光鸭云盘媒体根目录：/emby
STRM 输出目录：/opt/emby/strm/光鸭云盘
MoviePilot 地址（Emby 可访问）：http://moviepilot:3000
同步间隔：10
删除远端已不存在的旧 STRM：开启
```

`MoviePilot 地址` 必须从 **Emby 容器的视角** 能访问。MoviePilot 和 Emby 在同一个 Docker 网络时，可使用 MoviePilot 容器名和容器端口；否则使用 Emby 能访问的内网地址。

例如宿主机与容器映射：

```yaml
MoviePilot:
  - /opt/emby/strm:/opt/emby/strm

Emby:
  - /opt/emby/strm:/strm:ro
```

Emby 媒体库选择：

```text
/strm/光鸭云盘
```

## STRM 内容示例

远端文件：

```text
/emby/电视剧/无耻之徒/Season 01/Shameless.S01E01.mkv
```

生成：

```text
/opt/emby/strm/光鸭云盘/电视剧/无耻之徒/Season 01/Shameless.S01E01.strm
```

STRM 内容类似：

```text
http://moviepilot:3000/api/v1/plugin/GuangYaStrm/play?path=%2Femby%2F...&sig=<单文件签名>
```

插件内部保存随机 `stream_secret`，STRM 中只包含针对该文件路径生成的 HMAC 签名，不会写入 MoviePilot 管理员 Token，也不会直接暴露播放密钥。

## 安全设计

`/play` 路由允许 Emby 未登录 MoviePilot 时访问，但会执行以下校验：

- HMAC 单文件路径签名；
- 只允许配置的远端根目录（默认 `/emby`）以内的文件；
- 拒绝 `..` 路径；
- 只允许配置的视频扩展名；
- 同步和状态 API 仍要求 MoviePilot Bearer 登录认证。

修改 `stream_secret` 并重新同步后，旧 STRM URL 会全部失效。

## 当前版本的取舍

1.0.0 优先保证兼容性，播放请求复用 `ShukGuangYaDisk.stream_file()`，因此视频数据会经过 MoviePilot 所在服务器转发。

优点：

- 复用光鸭插件已经实现的签名 URL、Referer、Range、Content-Range 和 seek 逻辑；
- 不需要 rclone、WebDAV mount、CloudDrive2；
- 不需要本地媒体文件。

缺点：

- 播放会占用 MoviePilot 服务器带宽。

后续可以增加 302 签名直链模式，在确认光鸭签名地址可被 Emby 直接访问后，让 Emby 直接连接光鸭/CDN。

## 同步行为

每次同步执行远端完整 inventory + 本地增量写入：

- 新远端视频 → 新建 `.strm`；
- URL 或配置变化 → 更新 `.strm`；
- 未变化 → 不重写；
- 远端视频删除/移动 → 可选删除旧 `.strm`；
- 只删除插件自己索引记录过的 `.strm`，不会清理其它文件。

## 当前不会同步的内容

当前版本不会自动下载海报、NFO、外挂字幕和其它附件。嵌入视频文件中的音轨/字幕不受影响。
