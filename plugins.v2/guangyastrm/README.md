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
