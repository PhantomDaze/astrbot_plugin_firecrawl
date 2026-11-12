# astrbot_plugin_firecrawl

把**自建 Firecrawl** 接入 AstrBot：为模型提供联网搜索与网页正文提取两个工具，附带一个 `/firecrawl` 调试命令。

## 为什么需要这个插件

AstrBot 主程序已经内置 Firecrawl 支持（`web_search_firecrawl` 与 `firecrawl_extract_web_page`），但请求地址是硬编码的：

- `https://api.firecrawl.dev/v2/search`
- `https://api.firecrawl.dev/v2/scrape`

配置里只提供 API Key 列表，没有任何 base URL 选项，因此自建实例（默认 `http://127.0.0.1:3002`）无法被内置功能使用。插件市场里也没有能指向自建实例的现成实现。

本插件补齐这块：**工具参数与返回格式尽量与内置实现保持一致**，只把 endpoint 换成你在配置里填写的地址。

## 功能

| 能力 | 说明 |
| --- | --- |
| `firecrawl_search` | 对应内置 `web_search_firecrawl`，返回 `{"results": [{"title", "url", "snippet", "index"}]}` |
| `firecrawl_scrape` | 对应内置 `firecrawl_extract_web_page`，返回 `URL: ...\nContent: ...` |
| `/firecrawl` 命令 | `status` 查看实例连通性、`search 关键词` 手动搜索、`read URL` 读取正文 |
| API 版本自动探测 | 默认先打 `/v2`，遇到 404 自动回退 `/v1` |
| 多 Key 轮询 | 配置多个 Key 时轮流使用，401/402/403/429/5xx 自动切换下一个 |
| 内容截断 | 按配置的字符数截断，避免工具返回撑爆上下文 |

## 安装

```bash
cd /opt/bot/AstrBot/data/plugins   # 换成你的 AstrBot 插件目录
git clone https://github.com/PhantomDaze/astrbot_plugin_firecrawl.git
```

然后在 WebUI 的「插件」页重载插件（或重启 AstrBot），插件配置页会出现下面这些选项。

> 依赖只有 `aiohttp`，AstrBot 本体已自带；`requirements.txt` 仍然保留，便于独立安装。

## 配置

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `base_url` | string | `http://127.0.0.1:3002` | 自建实例地址，不要带 `/v1`、`/v2` 后缀 |
| `api_keys` | list | `[]` | 自建且 `USE_DB_AUTHENTICATION=false` 时留空 |
| `api_version` | string | `auto` | `auto` / `v2` / `v1` |
| `request_timeout` | int | `60` | 整体超时秒数 |
| `search_limit` | int | `5` | 模型未指定数量时的默认值 |
| `default_format` | string | `markdown` | `markdown` / `html` / `rawHtml` / `summary` |
| `only_main_content` | bool | `true` | 去掉导航、页脚等噪音 |
| `max_content_chars` | int | `20000` | 单次返回正文的字符上限 |
| `search_with_content` | bool | `false` | 搜索时顺带抓正文（返回体明显变大） |
| `verify_ssl` | bool | `true` | 自签名证书时关闭 |
| `command_enabled` | bool | `true` | 是否响应 `/firecrawl` 命令 |

### Docker 部署的注意事项

AstrBot 跑在容器里时，`127.0.0.1` 指容器自身。请把 `base_url` 换成：

- 宿主机内网地址，例如 `http://192.168.1.10:3002`；
- 或者 Docker Desktop 的 `http://host.docker.internal:3002`（Linux 上需要加 `extra_hosts`）。

## 使用

工具由模型自行调用，直接说人话即可：

```text
帮我搜一下 Firecrawl 自建部署的官方文档，列三条结果。
读一下 https://docs.firecrawl.dev/contributing/self-host 并总结部署步骤。
```

也可以手动验证：

```text
/firecrawl status
/firecrawl search AstrBot 插件开发
/firecrawl read https://example.com
```

## 自建 Firecrawl 的三个坑

1. **搜索依赖 SearXNG。** 自建版本的 `/search` 需要 `SEARXNG_ENDPOINT`，没有配置时只有 `/scrape` 能用。
2. **默认没有鉴权。** `USE_DB_AUTHENTICATION=false` 时谁都能调用，别把 3002 端口直接暴露到公网。
3. **版本差异。** 新版镜像提供 `/v2/search`、`/v2/scrape`；旧镜像只有 `/v1`。插件默认自动探测，但 v1 的 `sources`、`scrapeOptions` 等新参数不可用。

## 与内置 Firecrawl 的差异

| 项 | 内置 | 本插件 |
| --- | --- | --- |
| 请求地址 | 写死官方 API | 任意地址，可指向自建实例或官方云 |
| 工具名 | `web_search_firecrawl` / `firecrawl_extract_web_page` | `firecrawl_search` / `firecrawl_scrape` |
| 返回格式 | JSON 结果列表 / `URL + Content` | 保持一致 |
| 调试入口 | 无 | `/firecrawl status` 等 |
| 输出截断 | 无 | 可配置 |

把 `base_url` 设为 `https://api.firecrawl.dev` 并配置 Key，本插件同样可以调用官方云服务。

## 兼容性

- AstrBot：4.x（在 4.29.4 上实测通过）
- Firecrawl：`ghcr.io/firecrawl/firecrawl:latest`（build `675de6a`，2026-11-03）实测通过，v2 API
- Python：3.10+

## 开发与自测

```bash
python tests/smoke_test.py http://127.0.0.1:3002
```

脚本会依次验证 `ping`、`search`、`scrape`，并确认不可达地址会被正确报告为连接失败。

## 许可

MIT。工具的参数与返回格式参考了 AstrBot 主仓库 `astrbot/core/astr_agent_runtime/tools/web_search_tools.py` 的内置实现。
