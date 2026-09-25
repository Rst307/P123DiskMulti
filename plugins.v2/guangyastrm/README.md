# 光鸭 STRM（MoviePilot V2）

这是“光鸭云盘助手”的配套 STRM 插件，专门解决光鸭文件是 MoviePilot 远程存储、服务器本地并不存在 `/gy` 这类挂载目录的问题。

## 工作方式

```text
光鸭云盘 /emby/
       ↓
ShukGuangYaDisk.browse_path(recursion=True)
       ↓
GuangYaStrm
       ↓
/opt/emby/strm/光鸭云盘/**/*.strm
       ↓
Emby
       ↓
GuangYaStrm /play
       ↓ 仅换取 signedURL
302 Redirect
       ↓
光鸭 / CDN
```

不会把影片主体下载到服务器，本地只保存很小的 `.strm` 和索引文件。

## 前置条件

- MoviePilot V2
- 已安装并登录“光鸭云盘助手” `ShukGuangYaDisk`
- 光鸭中的媒体已经整理到例如 `/emby/`
- MoviePilot 对 STRM 输出目录有写权限
- Emby 能访问你填写的 MoviePilot 地址

## 推荐配置

```text
光鸭插件 ID：ShukGuangYaDisk
光鸭云盘媒体根目录：/emby
STRM 输出目录：/opt/emby/strm/光鸭云盘
MoviePilot 地址：http://moviepilot:3000
同步间隔：10 分钟
删除失效 STRM：开启
```

如果 MoviePilot 与 Emby 不在同一个 Docker 网络，请把 MoviePilot 地址填写成 Emby 实际能访问的内网地址。

## Emby 挂载示例

宿主机：

```text
/opt/emby/strm
```

MoviePilot：

```text
/opt/emby/strm:/opt/emby/strm
```

Emby：

```text
/opt/emby/strm:/strm:ro
```

Emby 媒体库选择：

```text
/strm/光鸭云盘
```

## 播放安全

V2 当前宿主支持插件 API 的 `allow_anonymous`。本插件的播放端点只对带有正确单文件 HMAC 签名的 URL 放行，并同时限制：

- 只能访问配置的光鸭媒体根目录
- 拒绝路径越界
- 只允许配置的视频扩展名
- 管理用同步/状态接口仍需要 MoviePilot 登录认证

因此 STRM 中不会保存 MoviePilot 管理员 Token。

## v1.0.2 播放链路

v1.0.2 已切换为 302 直链。

播放时 MoviePilot 只做两件事：

1. 校验 STRM 中的单文件 HMAC 签名；
2. 向光鸭 API 换取短期 signedURL，并向 Emby 返回 HTTP 302。

之后媒体数据由 Emby 直接访问光鸭/CDN，不再经过 MoviePilot 进程。

因此：

- 不占本地影视存储
- 不需要 rclone
- 不需要 CloudDrive2
- 不需要 WebDAV mount
- MoviePilot 只承担很小的 API/换链流量
- 视频主体流量走光鸭/CDN → Emby


## 自动整理（v1.1.0）

本插件现在可以像“123云盘多盘合并”的目录整理一样，直接扫描光鸭远端目录并提交到 MoviePilot 原生整理链。

推荐与你当前的目录结构这样配：

```text
待整理目录（源）：
光鸭云盘助手:/emby_raw

MoviePilot 媒体库/整理目标：
光鸭云盘助手:/emby
```

插件配置：

```text
启用光鸭云盘自动整理：开启
待整理光鸭目录：
/emby_raw

自动整理 Cron：
*/10 * * * *
```

工作流：

```text
光鸭 /emby_raw
      ↓ 递归扫描视频
GuangYaStrm
      ↓ TransferChain.manual_transfer(background=True)
MoviePilot 原生识别/命名/整理规则
      ↓
光鸭云盘助手 StorageOperSelection
      ↓ 云端 move/copy
光鸭 /emby
      ↓
STRM 同步
      ↓
Emby
```

需要注意：**“待整理目录”只负责指定源文件在哪里，最终整理到哪里由 MoviePilot 自己的整理/存储目录设置决定。** 因此请先在 MoviePilot 中把媒体库目标配置成“光鸭云盘助手”的 `/emby`。

自动整理具备：

- 多个源目录（每行一个）
- Cron 周期扫描
- 保存后立即执行一次
- 后台非重入，避免同时跑两轮
- 递归扫描视频文件
- 可跳过 BDMV / CERTIFICATE 原盘结构
- 调用 MoviePilot 原生整理链，不自己实现识别和命名规则
- 已成功移动出源目录的文件下次扫描不会再出现
- `/organize/status` 可查看最近扫描/提交/失败数量

手动 API：

```text
POST /api/v1/plugin/GuangYaStrm/organize/run
GET  /api/v1/plugin/GuangYaStrm/organize/status
```

这两个管理接口需要 MoviePilot Bearer 登录认证。


## 整理联动与立即操作（v1.2.0）

新增配置：

```text
整理完成后自动生成 STRM：开启/关闭
```

开启后，插件不会在“提交整理任务”时立刻生成 STRM，而是监听 MoviePilot 的 `TransferComplete` 事件。只有在 MoviePilot 真正完成整理，并且：

- 来源文件位于本插件配置的“待整理光鸭目录”；
- 目标存储仍然是“光鸭云盘助手”；
- 目标文件位于 STRM 媒体根目录（默认 `/emby`）；

才会立即生成该目标文件对应的 STRM。

典型流程：

```text
/emby_raw/乱文件.mkv
        ↓
立即整理 / Cron 自动整理
        ↓
MoviePilot 原生整理链
        ↓
光鸭云端移动到 /emby/...
        ↓ TransferComplete
GuangYaStrm 自动生成单文件 STRM
        ↓
/opt/emby/strm/光鸭云盘/...
```

插件详情页新增两个操作：

- **立即整理**：立即扫描“待整理光鸭目录”并后台提交到 MoviePilot 整理队列。这个手动操作即使没有开启 Cron 自动整理也可以使用。
- **立即生成 STRM**：立即后台完整扫描 STRM 媒体根目录并生成/更新 STRM，同时按配置清理失效 STRM。

管理 API：

```text
GET  /api/v1/plugin/GuangYaStrm/action/organize
GET  /api/v1/plugin/GuangYaStrm/action/sync
POST /api/v1/plugin/GuangYaStrm/organize/run
POST /api/v1/plugin/GuangYaStrm/sync
```

这些管理接口均需要 MoviePilot 登录认证。
