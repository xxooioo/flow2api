# 异步视频接口 `/v1/videos`

提交任务后立即返回任务 id，视频在后台生成，客户端轮询查询结果。字段兼容各家中转站常见写法，
New API 的 OpenAI 类型渠道可以原样转发，不需要额外插件。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/videos`（同 `/v1/video/generations`） | 提交任务 |
| GET | `/v1/videos/{id}`（同 `/v1/video/generations/{id}`） | 查询进度和结果 |
| GET / HEAD | `/v1/videos/{id}/content` | 下载 MP4 |

鉴权：`Authorization: Bearer <API Key>`。

## 模型

| 模型名 | 文生视频 | 首尾帧（1～2 张） | 参考图（1～3 张） | 时长 | 清晰度 |
|---|---|---|---|---|---|
| `veo-3.1-fast` | ✅ | ✅ | ✅ | 4 / 6 / 8 秒（4、6 秒仅 720p，参考图仅 8 秒） | 720p / 1080p / 4k |
| `veo-3.1-quality` | ✅ | ✅ | ❌ | 固定 8 秒 | 720p / 1080p / 4k |
| `veo-3.1-lite` | ✅ | ✅ | ✅ | 4 / 6 / 8 秒（参考图仅 8 秒） | 720p |
| `omni-1.1-flash` | ✅ | ❌ | ✅（0～3 张） | 4 / 6 / 8 / 10 秒，向上取整 | 720p |

- 模型名也接受 `gemini-` 前缀和常见别名（如 `gemini-veo-3.1-fast`、`veo3.1-fast`、`omni-1.1-flash-preview`）。
- 画面比例只有横屏 16:9 和竖屏 9:16；其他比例按宽高判断横竖。
- Fast / Lite 只有明确传 4 或 6 时才生成短视频，其他时长都按 8 秒；Quality 始终 8 秒。返回里的 `seconds` 是实际时长。
- 1080p / 4k 是先生成再放大，耗时更长。
- 账号层级自动处理：Ultra 账号用 Ultra 模型，Pro 账号自动降为标准模型。

## 请求字段

| 字段 | 说明 |
|---|---|
| `model` | 必填，见上表 |
| `prompt` | 必填，视频描述 |
| `duration` / `seconds` | 时长（秒），两者都传时以 `duration` 为准 |
| `aspect_ratio` / `ratio` | `16:9` 或 `9:16`（也接受 `landscape` / `portrait`） |
| `size` | 如 `1280x720`、`720x1280`；未传比例时用它判断横竖，未传清晰度时用它判断清晰度 |
| `resolution` | `720p`（默认）、`1080p`、`4k` |
| `reference_images` | 图片数组。元素可以是字符串，或 `{"url": "...", "role": "first_frame"}` / `{"url": "...", "role": "last_frame"}`；不带 role 的是普通参考图 |
| `first_frame_url` / `last_frame_url` | 首帧、尾帧（也接受 `first_frame` / `last_frame`） |
| `image_urls` / `images` / `input_reference` / `image` | 普通图片的兼容字段，按 `reference_images` → `image_urls` → `images` → `input_reference` → `image` 的顺序取第一个出现的字段，不合并 |
| `generation_type` / `mode` | `frame`（首尾帧）或 `reference`（参考图）；不传时按张数判断：1～2 张为首尾帧，3 张为参考图 |
| `metadata` | 以上字段也可以放在 `metadata` 里，例如 `metadata.resolution`、`metadata.generation_type` |

图片可以是：http(s) 链接、`data:image/png;base64,...` 或纯 base64，单张不超过 20MB。
直接调用 flow2api 时也支持 multipart 表单上传图片文件（字段名同上）；经过 New API 时请用 JSON。

## 示例

文生视频，竖屏 1080p：

```bash
curl https://你的域名/v1/videos \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "veo-3.1-fast", "prompt": "一只柴犬在樱花树下奔跑", "aspect_ratio": "9:16", "resolution": "1080p"}'
```

首尾帧：

```bash
curl https://你的域名/v1/videos \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3.1-fast",
    "prompt": "镜头平稳推进，从首帧自然过渡到尾帧",
    "reference_images": [
      {"url": "data:image/png;base64,<首帧BASE64>", "role": "first_frame"},
      {"url": "https://example.com/last.png", "role": "last_frame"}
    ]
  }'
```

Omni 参考图，6 秒：

```bash
curl https://你的域名/v1/videos \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "omni-1.1-flash", "prompt": "保留参考图的人物外观，在海边跳舞", "duration": 6, "images": ["https://example.com/person.png"]}'
```

查询与下载：

```bash
curl https://你的域名/v1/videos/<任务ID> -H "Authorization: Bearer $API_KEY"
curl -o video.mp4 https://你的域名/v1/videos/<任务ID>/content -H "Authorization: Bearer $API_KEY"
```

## 响应

```json
{
  "id": "video_9c6177b16e024f19937917a1",
  "object": "video",
  "model": "veo-3.1-fast",
  "status": "completed",
  "progress": 100,
  "created_at": 1789600000,
  "completed_at": 1789600090,
  "seconds": "8",
  "size": "1080x1920",
  "resolution": "1080p",
  "aspect_ratio": "9:16",
  "url": "https://你的域名/tmp/xxx.mp4",
  "metadata": {"url": "https://你的域名/tmp/xxx.mp4"},
  "output": [{"url": "https://你的域名/tmp/xxx.mp4"}]
}
```

- `status`：`queued` → `in_progress` → `completed` 或 `failed`；建议每 5 秒查询一次。
- 失败时带 `error.code` 和 `error.message`；参数错误在提交时直接返回 HTTP 400，不会创建任务。
- 任务在服务进程内执行，服务重启时未完成的任务会标记为失败，需要重新提交。

## 接入 New API

1. 新建渠道，类型选 **OpenAI**，Base URL 填 flow2api 地址（不带 `/v1`），密钥填 flow2api 的 `api_key`。
2. 模型填 `veo-3.1-fast`、`veo-3.1-quality`、`veo-3.1-lite`、`omni-1.1-flash`（想对外显示别的名字可以用模型重定向）。
3. 价格建议按次计费：New API 只从请求里读取时长和尺寸，客户端不传时长时按 4 秒计算。
4. 不要对这个渠道点"测试"：测试走聊天接口，会真的发起一次生成。
5. 用户调用 `https://你的NewAPI/v1/videos`，查询和下载也走 New API，字段与上面相同。

### 经过 New API 调用时的写法

New API（实测 v1.0.0-rc.45）转发自定义视频模型时只保留这些顶层字段：
`model`、`prompt`、`mode`、`image`、`images`（字符串数组）、`size`、`duration`、`seconds`、`input_reference`、`metadata`。
其他顶层字段（如 `aspect_ratio`、`resolution`、`reference_images`、`first_frame_url`）会被丢弃，
所以通过 New API 调用时请这样写：

| 需求 | 写法 |
|---|---|
| 竖屏 / 横屏 | `"size": "720x1280"` / `"1280x720"`，或 `"metadata": {"aspect_ratio": "9:16"}` |
| 清晰度 | `"metadata": {"resolution": "1080p"}` |
| 时长 | `"duration": 4` |
| 普通参考图 / 首尾帧 | `"images": ["首帧", "尾帧"]`（1～2 张为首尾帧，3 张为参考图） |
| 指定模式 | `"mode": "reference"` 或 `"mode": "frames"` |
| 带 role 的图片 | `"metadata": {"reference_images": [{"url": "...", "role": "first_frame"}, ...]}` |

示例（竖屏 1080p 首尾帧）：

```bash
curl https://你的NewAPI/v1/videos \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo-3.1-fast",
    "prompt": "镜头平稳推进，从首帧过渡到尾帧",
    "size": "720x1280",
    "images": ["data:image/png;base64,<首帧>", "https://example.com/last.png"],
    "metadata": {"resolution": "1080p"}
  }'
```
