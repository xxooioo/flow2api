"""Async video task API: request normalization, model selection and task storage.

Backs the OpenAI-style ``/v1/videos`` (and ``/v1/video/generations``) endpoints:
a client submits one public model name plus parameters, gets a task id back
immediately, and polls until the video is ready. The generation itself reuses
``GenerationHandler.handle_generation``.
"""

import asyncio
import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..core.database import Database
from ..core.logger import debug_logger
from .generation_handler import MODEL_CONFIG


class VideoRequestError(Exception):
    """A client-side problem with a video request (reported as HTTP 4xx)."""

    def __init__(self, message: str, code: str = "invalid_request", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


# ──────────────────────────────────────────────
# Public model names → model family
# ──────────────────────────────────────────────
FAMILY_ALIASES = {
    "veo-3.1-fast": "fast",
    "veo3.1-fast": "fast",
    "veo-3.1-fast-generate-preview": "fast",
    "veo-3.1-fast-generate-001": "fast",
    "veo-3.1-quality": "quality",
    "veo3.1-quality": "quality",
    "veo-3.1": "quality",
    "veo3.1": "quality",
    "veo-3.1-generate-preview": "quality",
    "veo-3.1-generate-001": "quality",
    "omni-1.1-flash": "omni",
    "omni-1.1-flash-preview": "omni",
    "omni": "omni",
}

# family → mode → (landscape, portrait) internal MODEL_CONFIG keys at 720p.
# Ultra keys are fine for Pro accounts: the generation handler downgrades them by tier.
VEO_MODELS = {
    "fast": {
        "text": ("veo_3_1_t2v_fast_ultra", "veo_3_1_t2v_fast_portrait_ultra"),
        "frames": ("veo_3_1_i2v_s_fast_ultra_fl", "veo_3_1_i2v_s_fast_portrait_ultra_fl"),
        "reference": ("veo_3_1_r2v_fast_ultra", "veo_3_1_r2v_fast_portrait_ultra"),
    },
    "quality": {
        "text": ("veo_3_1_t2v_landscape", "veo_3_1_t2v_portrait"),
        "frames": ("veo_3_1_i2v_s_landscape", "veo_3_1_i2v_s_portrait"),
    },
}
RESOLUTION_SUFFIX = {"720p": "", "1080p": "_1080p", "4k": "_4k"}
OMNI_SECONDS = (4, 6, 8, 10)
VEO_SECONDS = 8
MAX_REFERENCE_IMAGES = 3

VIDEO_SIZES = {
    ("720p", "landscape"): "1280x720",
    ("720p", "portrait"): "720x1280",
    ("1080p", "landscape"): "1920x1080",
    ("1080p", "portrait"): "1080x1920",
    ("4k", "landscape"): "3840x2160",
    ("4k", "portrait"): "2160x3840",
}

RESOLUTION_ALIASES = {
    "4k": "4k",
    "2160p": "4k",
    "2160": "4k",
    "uhd": "4k",
    "1080p": "1080p",
    "1080": "1080p",
    "fhd": "1080p",
    "720p": "720p",
    "720": "720p",
    "hd": "720p",
    "480p": "720p",
    "480": "720p",
    "sd": "720p",
    "auto": "720p",
    "low": "720p",
    "medium": "720p",
    "high": "720p",
}

FRAMES_MODES = {"frames", "frame", "first_last", "first_last_frame", "i2v", "image_to_video", "fl"}
REFERENCE_MODES = {"reference", "references", "r2v", "reference_to_video", "ingredients", "asset", "assets"}
FIRST_ROLES = {"first_frame", "first", "start", "start_frame"}
LAST_ROLES = {"last_frame", "last", "end", "end_frame"}

# Plain reference fields, in priority order. The first one present wins and
# fields are never merged (several clients send the same files under two names).
PLAIN_IMAGE_FIELDS = ("reference_images", "image_urls", "images", "image[]", "input_reference", "image")

RATIO_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*[:x×*]\s*(\d+(?:\.\d+)?)$")
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


@dataclass
class VideoJob:
    """A validated video request ready to run."""

    model: str
    internal_model: str
    prompt: str
    image_sources: List[str] = field(default_factory=list)
    mode: str = "text"
    seconds: int = VEO_SECONDS
    resolution: str = "720p"
    orientation: str = "landscape"

    @property
    def size(self) -> str:
        return VIDEO_SIZES[(self.resolution, self.orientation)]

    @property
    def aspect_ratio(self) -> str:
        return "9:16" if self.orientation == "portrait" else "16:9"


def resolve_family(model: str) -> Optional[str]:
    name = (model or "").strip().lower()
    if name.startswith("models/"):
        name = name[len("models/"):]
    if name.startswith("gemini-"):
        name = name[len("gemini-"):]
    return FAMILY_ALIASES.get(name)


def _lookup(body: Dict[str, Any], *keys: str) -> Any:
    """Return the first non-empty value among keys, checking body then body.metadata."""
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    for source in (body, metadata):
        for key in keys:
            value = source.get(key)
            if value is None or value == "":
                continue
            return value
    return None


def _orientation_from_value(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in ("portrait", "vertical"):
        return "portrait"
    if text in ("landscape", "horizontal"):
        return "landscape"
    match = RATIO_RE.match(text)
    if not match:
        return None
    width, height = float(match.group(1)), float(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return "portrait" if height > width else "landscape"


def _resolution_from_size(value: Any) -> Optional[str]:
    match = RATIO_RE.match(str(value or "").strip().lower())
    if not match or ":" in str(value):
        return None
    longest = max(float(match.group(1)), float(match.group(2)))
    if longest >= 3840:
        return "4k"
    if longest >= 1920:
        return "1080p"
    return "720p"


def _normalize_resolution(value: Any) -> str:
    text = str(value).strip().lower().replace(" ", "")
    resolution = RESOLUTION_ALIASES.get(text)
    if resolution is None:
        raise VideoRequestError(f"不支持的清晰度: {value}，可选 720p、1080p、4k", code="invalid_resolution")
    return resolution


def _parse_seconds(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        seconds = int(float(str(value).strip().rstrip("s")))
    except ValueError:
        raise VideoRequestError(f"时长必须是数字: {value}", code="invalid_duration")
    if seconds <= 0:
        raise VideoRequestError(f"时长必须大于 0: {value}", code="invalid_duration")
    return seconds


def _image_source(item: Any) -> Tuple[Optional[str], Optional[str]]:
    """Normalize one image entry to (source, role); source is a data URL or http(s) URL."""
    role = None
    if isinstance(item, dict):
        role = str(item.get("role") or item.get("type") or "").strip().lower() or None
        if item.get("b64_json") or item.get("data"):
            item = item.get("b64_json") or item.get("data")
        else:
            url = item.get("url") or item.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            item = url
    if not isinstance(item, str) or not item.strip():
        return None, role
    source = item.strip()
    if source.startswith(("data:", "http://", "https://")):
        return source, role
    if BASE64_RE.match(source[:200]):
        compact = re.sub(r"\s+", "", source)
        return f"data:image/png;base64,{compact}", role
    raise VideoRequestError("图片必须是 http(s) 链接、data URL 或 base64", code="invalid_image")


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _collect_images(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], List[str]]:
    """Return (first_frame, last_frame, plain_references)."""
    first = _lookup(body, "first_frame_url", "first_frame", "firstFrame", "start_image")
    last = _lookup(body, "last_frame_url", "last_frame", "lastFrame", "end_image")
    first = _image_source(_as_list(first)[0])[0] if first is not None else None
    last = _image_source(_as_list(last)[0])[0] if last is not None else None

    plain: List[str] = []
    for key in PLAIN_IMAGE_FIELDS:
        value = _lookup(body, key)
        if value is None:
            continue
        for item in _as_list(value):
            source, role = _image_source(item)
            if not source:
                continue
            if role in FIRST_ROLES:
                first = first or source
            elif role in LAST_ROLES:
                last = last or source
            else:
                plain.append(source)
        break
    return first, last, plain


def _requested_mode(body: Dict[str, Any]) -> Optional[str]:
    value = _lookup(body, "generation_type", "mode", "generationType")
    text = str(value or "").strip().lower()
    if text in FRAMES_MODES:
        return "frames"
    if text in REFERENCE_MODES:
        return "reference"
    return None


def parse_video_request(body: Dict[str, Any]) -> VideoJob:
    """Validate a submit body and pick the internal model. Raises VideoRequestError."""
    if not isinstance(body, dict):
        raise VideoRequestError("请求体必须是 JSON 对象")
    model = str(body.get("model") or "").strip()
    if not model:
        raise VideoRequestError("缺少 model 参数", code="missing_model")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise VideoRequestError("缺少 prompt 参数", code="missing_prompt")

    family = resolve_family(model)
    seconds = _parse_seconds(_lookup(body, "duration", "seconds", "durationSeconds"))
    size = _lookup(body, "size")
    orientation = (
        _orientation_from_value(_lookup(body, "aspect_ratio", "aspectRatio", "ratio"))
        or _orientation_from_value(size)
        or "landscape"
    )
    raw_resolution = _lookup(body, "resolution", "resolution_name", "resolutionName")
    resolution = _normalize_resolution(raw_resolution) if raw_resolution is not None else (_resolution_from_size(size) or "720p")
    first, last, plain = _collect_images(body)
    requested_mode = _requested_mode(body)

    if family is None:
        config = MODEL_CONFIG.get(model)
        if not config or config.get("type") != "video":
            supported = "veo-3.1-fast、veo-3.1-quality、omni-1.1-flash"
            raise VideoRequestError(f"不支持的视频模型: {model}，可用: {supported}", code="model_not_found", status_code=404)
        images = ([first] if first else []) + ([last] if last else []) + plain
        return VideoJob(
            model=model,
            internal_model=model,
            prompt=prompt,
            image_sources=images,
            mode=config.get("video_type", "text"),
            seconds=seconds or VEO_SECONDS,
            resolution=resolution,
            orientation=orientation,
        )

    if family == "omni":
        if first or last:
            raise VideoRequestError("omni-1.1-flash 只支持参考图（最多 3 张），不支持首尾帧", code="unsupported_mode")
        if len(plain) > MAX_REFERENCE_IMAGES:
            raise VideoRequestError(f"omni-1.1-flash 最多 {MAX_REFERENCE_IMAGES} 张参考图", code="too_many_images")
        if resolution != "720p":
            raise VideoRequestError("omni-1.1-flash 只支持 720p", code="invalid_resolution")
        wanted = seconds or VEO_SECONDS
        omni_seconds = next((s for s in OMNI_SECONDS if wanted <= s), OMNI_SECONDS[-1])
        return VideoJob(
            model=model,
            internal_model=f"omni-1.1-flash-{omni_seconds}s-{orientation}",
            prompt=prompt,
            image_sources=plain,
            mode="reference" if plain else "text",
            seconds=omni_seconds,
            resolution=resolution,
            orientation=orientation,
        )

    if first or last:
        if not first:
            raise VideoRequestError("提供尾帧时必须同时提供首帧", code="missing_first_frame")
        if requested_mode == "reference":
            raise VideoRequestError("首尾帧和参考图模式不能同时使用", code="unsupported_mode")
        mode = "frames"
        images = [first] + ([last] if last else [])
    elif plain:
        if requested_mode == "reference" or (requested_mode is None and len(plain) > 2):
            mode = "reference"
            if len(plain) > MAX_REFERENCE_IMAGES:
                raise VideoRequestError(f"参考图最多 {MAX_REFERENCE_IMAGES} 张", code="too_many_images")
        else:
            mode = "frames"
            if len(plain) > 2:
                raise VideoRequestError("首尾帧模式最多 2 张图（首帧、尾帧）；参考图模式请设置 generation_type=reference", code="too_many_images")
        images = plain
    else:
        mode = "text"
        images = []

    variants = VEO_MODELS[family].get(mode)
    if variants is None:
        raise VideoRequestError(f"{model} 不支持参考图模式，请改用首尾帧或 veo-3.1-fast", code="unsupported_mode")
    internal_model = variants[orientation == "portrait"] + RESOLUTION_SUFFIX[resolution]
    if internal_model not in MODEL_CONFIG:
        raise VideoRequestError(f"{model} 的这种模式不支持 {resolution}", code="invalid_resolution")
    return VideoJob(
        model=model,
        internal_model=internal_model,
        prompt=prompt,
        image_sources=images,
        mode=mode,
        seconds=VEO_SECONDS,
        resolution=resolution,
        orientation=orientation,
    )


def decode_data_url(source: str) -> bytes:
    header, _, data = source.partition(",")
    if not data or ";base64" not in header:
        raise VideoRequestError("data URL 必须是 base64 编码", code="invalid_image")
    try:
        return base64.b64decode(data, validate=False)
    except ValueError:
        raise VideoRequestError("图片 base64 解码失败", code="invalid_image")


# ──────────────────────────────────────────────
# Task storage
# ──────────────────────────────────────────────
TASK_COLUMNS = (
    "id", "model", "internal_model", "status", "progress", "prompt", "mode", "seconds",
    "size", "resolution", "aspect_ratio", "video_url", "media_id", "error_code",
    "error_message", "created_at", "completed_at",
)


class VideoTaskStore:
    """Persists video tasks in the main SQLite database (table ``video_tasks``)."""

    def __init__(self, db: Database):
        self.db = db
        self._ready = False
        self._init_lock = asyncio.Lock()

    async def _ensure_table(self):
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return
            async with self.db._connect(write=True) as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS video_tasks (
                        id TEXT PRIMARY KEY,
                        model TEXT NOT NULL,
                        internal_model TEXT NOT NULL,
                        status TEXT NOT NULL,
                        progress INTEGER NOT NULL DEFAULT 0,
                        prompt TEXT,
                        mode TEXT,
                        seconds INTEGER,
                        size TEXT,
                        resolution TEXT,
                        aspect_ratio TEXT,
                        video_url TEXT,
                        media_id TEXT,
                        error_code TEXT,
                        error_message TEXT,
                        created_at INTEGER NOT NULL,
                        completed_at INTEGER
                    )
                    """
                )
                # Tasks run inside this process; any left unfinished by a previous run are lost.
                await conn.execute(
                    """
                    UPDATE video_tasks
                    SET status = 'failed', error_code = 'interrupted',
                        error_message = '服务重启，任务已中断，请重新提交', completed_at = ?
                    WHERE status IN ('queued', 'in_progress')
                    """,
                    (int(time.time()),),
                )
                await conn.commit()
            self._ready = True

    async def create(self, job: VideoJob) -> Dict[str, Any]:
        await self._ensure_table()
        task = {
            "id": f"video_{uuid.uuid4().hex[:24]}",
            "model": job.model,
            "internal_model": job.internal_model,
            "status": "queued",
            "progress": 0,
            "prompt": job.prompt,
            "mode": job.mode,
            "seconds": job.seconds,
            "size": job.size,
            "resolution": job.resolution,
            "aspect_ratio": job.aspect_ratio,
            "video_url": None,
            "media_id": None,
            "error_code": None,
            "error_message": None,
            "created_at": int(time.time()),
            "completed_at": None,
        }
        placeholders = ", ".join("?" for _ in TASK_COLUMNS)
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                f"INSERT INTO video_tasks ({', '.join(TASK_COLUMNS)}) VALUES ({placeholders})",
                tuple(task[column] for column in TASK_COLUMNS),
            )
            await conn.commit()
        return task

    async def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_table()
        async with self.db._connect() as conn:
            cursor = await conn.execute(
                f"SELECT {', '.join(TASK_COLUMNS)} FROM video_tasks WHERE id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
        return dict(zip(TASK_COLUMNS, row)) if row else None

    async def update(self, task_id: str, **fields: Any):
        await self._ensure_table()
        columns = [column for column in fields if column in TASK_COLUMNS and column != "id"]
        if not columns:
            return
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                f"UPDATE video_tasks SET {', '.join(f'{c} = ?' for c in columns)} WHERE id = ?",
                tuple(fields[c] for c in columns) + (task_id,),
            )
            await conn.commit()


def build_task_response(task: Dict[str, Any]) -> Dict[str, Any]:
    """Render a task the way OpenAI-style video clients (and New API) expect."""
    response: Dict[str, Any] = {
        "id": task["id"],
        "task_id": task["id"],
        "object": "video",
        "model": task["model"],
        "status": task["status"],
        "progress": task["progress"],
        "created_at": task["created_at"],
        "seconds": str(task["seconds"]),
        "duration": task["seconds"],
        "size": task["size"],
        "resolution": task["resolution"],
        "aspect_ratio": task["aspect_ratio"],
    }
    if task.get("completed_at"):
        response["completed_at"] = task["completed_at"]
    if task["status"] == "completed" and task.get("video_url"):
        url = task["video_url"]
        response.update(
            {
                "url": url,
                "video_url": url,
                "metadata": {"url": url},
                "output": [{"url": url}],
            }
        )
    if task["status"] == "failed":
        response["error"] = {
            "code": task.get("error_code") or "video_generation_failed",
            "message": task.get("error_message") or "视频生成失败",
        }
    return response


# ──────────────────────────────────────────────
# Background execution
# ──────────────────────────────────────────────
VIDEO_TAG_RE = re.compile(r"<video[^>]+src=['\"]([^'\"]+)['\"]", re.IGNORECASE)
MEDIA_ID_RE = re.compile(r"data-media-id=['\"]([^'\"]*)['\"]", re.IGNORECASE)
PROGRESS_RE = re.compile(r"(\d{1,3})\s*%")


def _chunk_text(chunk: str) -> str:
    try:
        payload = json.loads(chunk[len("data: "):].strip())
    except json.JSONDecodeError:
        return ""
    choices = payload.get("choices") or [{}]
    delta = choices[0].get("delta") or {}
    return f"{delta.get('reasoning_content') or ''}{delta.get('content') or ''}"


class VideoTaskRunner:
    """Runs submitted jobs in the background and records their progress."""

    def __init__(self, store: VideoTaskStore):
        self.store = store
        self._running: set = set()

    def start(self, handler, task_id: str, job: VideoJob, images: List[bytes], base_url: Optional[str]):
        running = asyncio.create_task(self._run(handler, task_id, job, images, base_url))
        self._running.add(running)
        running.add_done_callback(self._running.discard)

    async def _run(self, handler, task_id: str, job: VideoJob, images: List[bytes], base_url: Optional[str]):
        progress = 5
        video_url = None
        media_id = None
        error_message = None
        error_code = "video_generation_failed"
        await self.store.update(task_id, status="in_progress", progress=progress)
        try:
            async for chunk in handler.handle_generation(
                model=job.internal_model,
                prompt=job.prompt,
                images=images or None,
                stream=True,
                base_url_override=base_url,
            ):
                if not chunk.startswith("data: "):
                    try:
                        payload = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    error = payload.get("error") if isinstance(payload, dict) else None
                    if error:
                        error_message = error.get("message") or str(error)
                        if int(error.get("status_code") or 500) < 500:
                            error_code = "invalid_request"
                    continue
                text = _chunk_text(chunk)
                found = VIDEO_TAG_RE.search(text)
                if found:
                    video_url = found.group(1)
                    media = MEDIA_ID_RE.search(text)
                    media_id = media.group(1) if media else None
                    continue
                for match in PROGRESS_RE.finditer(text):
                    value = min(int(match.group(1)), 99)
                    if value > progress:
                        progress = value
                        await self.store.update(task_id, progress=progress)
        except Exception as exc:
            error_message = f"视频生成异常: {exc}"
            debug_logger.log_error(f"[VIDEO TASK] {task_id} 异常: {exc}")

        finished_at = int(time.time())
        if video_url:
            await self.store.update(
                task_id,
                status="completed",
                progress=100,
                video_url=video_url,
                media_id=media_id,
                completed_at=finished_at,
            )
            debug_logger.log_info(f"[VIDEO TASK] {task_id} 完成: {video_url}")
            return
        await self.store.update(
            task_id,
            status="failed",
            progress=100,
            error_code=error_code,
            error_message=error_message or "生成结束但没有返回视频",
            completed_at=finished_at,
        )
        debug_logger.log_warning(f"[VIDEO TASK] {task_id} 失败: {error_message}")
