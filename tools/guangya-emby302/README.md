# GuangYa Emby 302 Gateway

这是 `GuangYaStrm` v1.3.0 的外网直链伴侣服务。

目的不是“再加一层代理”，而是让 Cloudflare Tunnel 只承载 Emby 页面/API，视频主体绕过 Tunnel：

```text
外网客户端
   │
   │ HTTPS
   ▼
Cloudflare Tunnel
   │
   ▼
GuangYa Emby302 :8091
   │
   ├─ 页面 / API / WebSocket ──→ Emby :8096
   │
   └─ /Videos/{id}/stream...
          │
          ├─ 查询 Emby PlaybackInfo
          ├─ 找到 .strm
          ├─ 内网请求 MoviePilot GuangYaStrm /play
          ├─ 取得光鸭 signedURL
          └─ HTTP 302 ─────────────→ 外网客户端

随后：
外网客户端 ←──────── 光鸭 / CDN
```

所以大视频不经过 Emby、MoviePilot、本服务，也不经过 Cloudflare Tunnel。

## 为什么需要这个服务

Emby 的 STRM 本身并不保证把远程 URL 直接交给播放器。官方客户端通常仍请求 Emby 的 `/Videos/{Id}/stream`，Emby 再去读取 STRM 远端内容。

GuangYaStrm v1.0.2 以后虽然已经让“MoviePilot → 光鸭”变成 302，但如果外网客户端仍然连接 Emby 的 stream 接口，最后一段流量仍会变成：

```text
光鸭 → Emby → Cloudflare Tunnel → 客户端
```

本服务拦截 Emby 的直接视频流请求，让最终链路变成：

```text
光鸭 / CDN → 客户端
```

## 部署

进入此目录：

```bash
cd tools/guangya-emby302
```

复制示例：

```bash
cp docker-compose.example.yml docker-compose.yml
```

确认宿主机的 STRM 根目录。默认示例是：

```text
/opt/emby/strm
```

然后：

```bash
docker compose up -d --build
```

检查：

```text
http://服务器IP:8091/__guangya302/health
```

正常会返回 `"ok": true`。

## Cloudflare Tunnel

原来如果是：

```text
emby.example.com
    ↓
http://emby:8096
```

改成：

```text
emby.example.com
    ↓
http://guangya-emby302:8091
```

或者如果 cloudflared 访问的是宿主机：

```text
http://宿主机地址:8091
```

**不要把光鸭 signedURL 再套 Cloudflare。**

## Docker 网络

示例 compose 默认通过宿主机映射端口访问：

```text
EMBY_ORIGIN=http://host.docker.internal:8096
MOVIEPILOT_ORIGIN=http://host.docker.internal:3000
```

如果三个容器在同一个 Docker network，推荐直接改为：

```text
EMBY_ORIGIN=http://emby:8096
MOVIEPILOT_ORIGIN=http://moviepilot:3000
```

并把本服务加入相同 network。

## STRM 路径映射

假设 Emby 内看到：

```text
/strm/光鸭云盘/电影/...
```

本服务也应能在同一路径读到对应 .strm：

```yaml
volumes:
  - /opt/emby/strm:/strm:ro
```

默认：

```text
EMBY_STRM_PREFIX=/strm
LOCAL_STRM_PREFIX=/strm
```

如果 Emby 与本服务的挂载路径不同，可以分别修改这两个变量。

## Emby API Key

通常**不需要单独配置**。

网关会优先复用当前客户端请求里的 Emby token 调用 `PlaybackInfo`。只有某些客户端没有把 token 带到视频请求时，才需要额外设置：

```yaml
EMBY_API_KEY: "你的 Emby API Key"
```

它只在容器内部使用，不会返回给客户端。

## Cloudflare-only 模式

默认：

```text
ONLY_CF=true
```

只有请求头存在 `CF-Connecting-IP` 时才尝试 302。

因此：

- 外网 Cloudflare Tunnel：光鸭 302 直链
- 局域网直接访问 Emby / 8096：保持原行为
- 局域网直接访问 8091：默认也回退到 Emby

如果希望所有通过 8091 的客户端都尝试 302：

```text
ONLY_CF=false
```

## 转码处理

网关只拦截 Emby 的 `/Videos/{Id}/stream` / `original` 直接视频流接口。

`master.m3u8` 等 HLS/转码接口不会被拦截，因此需要转码时仍然是：

```text
光鸭 → Emby 转码 → 客户端
```

这属于预期行为。要完全避开服务器大流量，客户端需要能够 Direct Play 原文件。

## 成功时日志

播放外网 STRM 时应该看到：

```text
302 direct item=... client=... target_host=...
```

响应还会带：

```text
X-GuangYa-302: direct
```

而视频开始后，`guangya-emby302`、Emby 和 Cloudflare Tunnel 都不应该持续出现与视频码率相当的大流量。

## 回退策略

以下情况自动回退到原 Emby，不强行 302：

- 不是 GuangYaStrm 生成的 STRM
- Emby 无法返回 PlaybackInfo
- STRM 文件没有挂载到本服务
- MoviePilot 换链失败
- 请求明显需要转码
- 目标 URL 不是 HTTPS（默认策略）
- ONLY_CF=true 且请求不是从 Cloudflare 进入

所以它可以作为 Emby 的统一入口，而不用为普通本地媒体单独分域名。
