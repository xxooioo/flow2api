"""OpenAI-style async video endpoints: /v1/videos and its /v1/video/generations alias.

Submit returns a task id at once; the video is generated in the background and
polled with GET. Fields follow the common relay conventions (see
docs/videos-api.md), so New API's OpenAI channel can forward requests unchanged.
"""

import base64
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile

from ..core.auth import verify_api_key_flexible
from ..core.database import Database
from ..core.logger import debug_logger
from ..services.video_tasks import (
    VideoRequestError,
    VideoTaskRunner,
    VideoTaskStore,
    build_task_response,
    decode_data_url,
    parse_video_request,
)
from . import routes

router = APIRouter()

MAX_IMAGE_BYTES = 20 * 1024 * 1024

_store: Optional[VideoTaskStore] = None
_runner: Optional[VideoTaskRunner] = None


def set_dependencies(db: Database):
    """Bind the task store to the application database."""
    global _store, _runner
    _store = VideoTaskStore(db)
    _runner = VideoTaskRunner(_store)


def _require_store() -> VideoTaskStore:
    if _store is None:
        raise HTTPException(status_code=500, detail="Video task store not initialized")
    return _store


def _error(status_code: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error" if status_code < 500 else "server_error",
                "code": code,
            }
        },
    )


async def _read_body(request: Request) -> Dict[str, Any]:
    """Read a JSON body, or a multipart form with files turned into data URLs."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        body: Dict[str, Any] = {}
        async with request.form() as form:
            for key, value in form.multi_items():
                if isinstance(value, UploadFile):
                    data = await value.read()
                    if data:
                        mime = value.content_type or "image/png"
                        encoded = base64.b64encode(data).decode("ascii")
                        body.setdefault(key, []).append(f"data:{mime};base64,{encoded}")
                elif key not in body:
                    body[key] = value
        if isinstance(body.get("metadata"), str):
            try:
                body["metadata"] = json.loads(body["metadata"])
            except json.JSONDecodeError:
                raise VideoRequestError("metadata 必须是 JSON 对象字符串")
        return body
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise VideoRequestError("请求体必须是 JSON")


async def _load_images(sources: List[str]) -> List[bytes]:
    images: List[bytes] = []
    for source in sources:
        if source.startswith("data:"):
            data = decode_data_url(source)
        else:
            try:
                data = await routes._load_image_bytes_from_uri(source)
            except HTTPException:
                raise VideoRequestError(f"参考图下载失败: {source}", code="invalid_image")
        if not data:
            raise VideoRequestError("参考图内容为空", code="invalid_image")
        if len(data) > MAX_IMAGE_BYTES:
            raise VideoRequestError("单张参考图不能超过 20MB", code="image_too_large")
        images.append(data)
    return images


@router.post("/v1/videos")
@router.post("/v1/video/generations")
async def create_video(request: Request, api_key: str = Depends(verify_api_key_flexible)):
    """Submit a video task and return immediately with its id."""
    store = _require_store()
    try:
        body = await _read_body(request)
        job = parse_video_request(body)
        images = await _load_images(job.image_sources)
    except VideoRequestError as exc:
        return _error(exc.status_code, exc.message, exc.code)

    handler = routes._ensure_generation_handler()
    task = await store.create(job)
    _runner.start(handler, task["id"], job, images, routes._get_request_base_url(request))
    debug_logger.log_info(
        f"[VIDEO TASK] {task['id']} 已提交: {job.model} → {job.internal_model}, "
        f"mode={job.mode}, images={len(images)}"
    )
    return build_task_response(task)


@router.get("/v1/videos/{task_id}")
@router.get("/v1/video/generations/{task_id}")
async def get_video(task_id: str, api_key: str = Depends(verify_api_key_flexible)):
    """Return the current state of a video task."""
    task = await _require_store().get(task_id)
    if not task:
        return _error(404, f"任务不存在: {task_id}", "task_not_found")
    return build_task_response(task)


def _cached_file(url: str) -> Optional[Path]:
    """Map a flow2api cache URL (…/tmp/<file>) to the local file, if it is still there."""
    file_cache = getattr(routes.generation_handler, "file_cache", None)
    path = urlparse(url).path
    if not file_cache or "/tmp/" not in path:
        return None
    cache_dir = Path(file_cache.cache_dir).resolve()
    candidate = (cache_dir / path.split("/tmp/")[-1]).resolve()
    if candidate.parent != cache_dir or not candidate.is_file():
        return None
    return candidate


async def _proxy_video(url: str) -> Response:
    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=300.0))
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return _error(502, f"视频下载失败: {exc}", "video_download_failed")
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        return _error(502, f"视频下载失败（HTTP {upstream.status_code}）", "video_download_failed")

    async def stream_body():
        try:
            async for part in upstream.aiter_bytes():
                yield part
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {}
    if upstream.headers.get("content-length"):
        headers["Content-Length"] = upstream.headers["content-length"]
    return StreamingResponse(
        stream_body(),
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=headers,
    )


@router.api_route("/v1/videos/{task_id}/content", methods=["GET", "HEAD"])
@router.api_route("/v1/video/generations/{task_id}/content", methods=["GET", "HEAD"])
async def get_video_content(task_id: str, request: Request, api_key: str = Depends(verify_api_key_flexible)):
    """Download the finished MP4."""
    task = await _require_store().get(task_id)
    if not task:
        return _error(404, f"任务不存在: {task_id}", "task_not_found")
    if task["status"] != "completed" or not task.get("video_url"):
        return _error(404, "视频尚未生成完成", "video_not_ready")

    local_file = _cached_file(task["video_url"])
    if local_file:
        return FileResponse(local_file, media_type="video/mp4", filename=f"{task_id}.mp4")
    if request.method == "HEAD":
        return Response(status_code=200, media_type="video/mp4")
    return await _proxy_video(task["video_url"])
