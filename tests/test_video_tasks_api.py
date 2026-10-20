import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import routes, videos
from src.core.auth import verify_api_key_flexible
from src.core.database import Database
from src.services.video_tasks import VideoRequestError, parse_video_request

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class ParseVideoRequestTests(unittest.TestCase):
    def test_fast_text_portrait_1080p(self):
        job = parse_video_request(
            {"model": "veo-3.1-fast", "prompt": "cat", "aspect_ratio": "9:16", "resolution": "1080p"}
        )
        self.assertEqual(job.internal_model, "veo_3_1_t2v_fast_portrait_ultra_1080p")
        self.assertEqual(job.mode, "text")
        self.assertEqual(job.size, "1080x1920")

    def test_role_based_first_last_frames(self):
        job = parse_video_request(
            {
                "model": "gemini-veo-3.1-fast",
                "prompt": "move",
                "reference_images": [
                    {"url": PNG, "role": "first_frame"},
                    {"url": "https://example.com/last.png", "role": "last_frame"},
                ],
            }
        )
        self.assertEqual(job.internal_model, "veo_3_1_i2v_s_fast_ultra_fl")
        self.assertEqual(job.image_sources, [PNG, "https://example.com/last.png"])

    def test_explicit_frame_url_fields(self):
        job = parse_video_request(
            {
                "model": "veo-3.1-quality",
                "prompt": "move",
                "first_frame_url": "https://example.com/a.png",
                "last_frame_url": "https://example.com/b.png",
                "size": "720x1280",
            }
        )
        self.assertEqual(job.internal_model, "veo_3_1_i2v_s_portrait")
        self.assertEqual(job.image_sources, ["https://example.com/a.png", "https://example.com/b.png"])

    def test_image_count_picks_frames_or_reference(self):
        two = parse_video_request({"model": "veo-3.1-fast", "prompt": "p", "images": [PNG, PNG]})
        self.assertEqual(two.mode, "frames")
        three = parse_video_request({"model": "veo-3.1-fast", "prompt": "p", "image_urls": [PNG, PNG, PNG]})
        self.assertEqual(three.internal_model, "veo_3_1_r2v_fast_ultra")
        one_ref = parse_video_request(
            {"model": "veo-3.1-fast", "prompt": "p", "images": [PNG], "metadata": {"generation_type": "reference"}}
        )
        self.assertEqual(one_ref.mode, "reference")

    def test_metadata_resolution_and_raw_base64(self):
        raw = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
        job = parse_video_request(
            {"model": "veo3.1-fast", "prompt": "p", "input_reference": raw, "metadata": {"resolution": "4k"}}
        )
        self.assertEqual(job.internal_model, "veo_3_1_i2v_s_fast_ultra_fl_4k")
        self.assertTrue(job.image_sources[0].startswith("data:image/png;base64,"))

    def test_first_present_image_field_wins(self):
        job = parse_video_request(
            {"model": "veo-3.1-fast", "prompt": "p", "images": [PNG], "image[]": [PNG, PNG, PNG]}
        )
        self.assertEqual(len(job.image_sources), 1)

    def test_omni_rounds_duration_and_takes_references(self):
        job = parse_video_request(
            {"model": "omni-1.1-flash", "prompt": "p", "duration": 5, "images": [PNG, PNG]}
        )
        self.assertEqual(job.internal_model, "omni-1.1-flash-6s-landscape")
        self.assertEqual(job.seconds, 6)
        self.assertEqual(job.mode, "reference")

    def test_rejections(self):
        cases = [
            {"model": "veo-3.1-quality", "prompt": "p", "images": [PNG, PNG, PNG]},
            {"model": "omni-1.1-flash", "prompt": "p", "first_frame_url": PNG},
            {"model": "omni-1.1-flash", "prompt": "p", "resolution": "1080p"},
            {"model": "veo-3.1-fast", "prompt": "p", "last_frame_url": PNG},
            {"model": "veo-3.1-fast", "prompt": "p", "images": [PNG] * 4, "generation_type": "reference"},
            {"model": "veo-3.1-fast", "prompt": "p", "resolution": "2k"},
            {"model": "veo-3.1-fast", "prompt": ""},
            {"model": "no-such-model", "prompt": "p"},
        ]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(VideoRequestError):
                parse_video_request(body)


class FakeHandler:
    def __init__(self, cache_dir: Path, fail: bool = False):
        self.file_cache = SimpleNamespace(cache_dir=cache_dir)
        self.fail = fail
        self.calls = []

    async def handle_generation(self, model, prompt, images=None, stream=False, base_url_override=None, video_media_id=None):
        self.calls.append({"model": model, "prompt": prompt, "images": images, "stream": stream})
        progress = {"choices": [{"delta": {"reasoning_content": "生成进度: 40%\n"}}]}
        yield f"data: {json.dumps(progress, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.05)
        if self.fail:
            yield json.dumps({"error": {"message": "PUBLIC_ERROR_UNUSUAL_ACTIVITY", "status_code": 502}})
            return
        final = {
            "choices": [
                {
                    "delta": {"content": "<video src='http://testserver/tmp/abc.mp4' data-media-id='m1' controls></video>"},
                    "finish_reason": "stop",
                }
            ]
        }
        yield f"data: {json.dumps(final)}\n\n"


class VideoEndpointTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        root = Path(self._temp_dir.name)
        self.cache_dir = root / "tmp"
        self.cache_dir.mkdir()
        (self.cache_dir / "abc.mp4").write_bytes(b"fake-mp4-bytes")
        self.db = Database(db_path=str(root / "flow.db"))
        self._previous_handler = routes.generation_handler
        videos.set_dependencies(self.db)
        app = FastAPI()
        app.include_router(videos.router)
        app.dependency_overrides[verify_api_key_flexible] = lambda: "test-key"
        self.app = app

    def tearDown(self):
        routes.generation_handler = self._previous_handler
        self._temp_dir.cleanup()

    def _wait_for(self, client, task_id, status):
        for _ in range(100):
            body = client.get(f"/v1/videos/{task_id}").json()
            if body["status"] == status:
                return body
            time.sleep(0.02)
        self.fail(f"task never reached {status}: {body}")

    def test_submit_poll_and_download(self):
        handler = FakeHandler(self.cache_dir)
        routes.set_generation_handler(handler)
        with TestClient(self.app) as client:
            created = client.post(
                "/v1/video/generations",
                json={"model": "veo-3.1-fast", "prompt": "cat", "aspect_ratio": "9:16", "images": [PNG]},
            )
            self.assertEqual(created.status_code, 200)
            task = created.json()
            self.assertEqual(task["status"], "queued")
            self.assertEqual(task["size"], "720x1280")

            done = self._wait_for(client, task["id"], "completed")
            self.assertEqual(done["progress"], 100)
            self.assertEqual(done["metadata"]["url"], "http://testserver/tmp/abc.mp4")
            self.assertEqual(done["output"][0]["url"], done["url"])

            content = client.get(f"/v1/videos/{task['id']}/content")
            self.assertEqual(content.status_code, 200)
            self.assertEqual(content.content, b"fake-mp4-bytes")
            self.assertEqual(client.head(f"/v1/videos/{task['id']}/content").status_code, 200)

        self.assertEqual(handler.calls[0]["model"], "veo_3_1_i2v_s_fast_portrait_ultra_fl")
        self.assertEqual(len(handler.calls[0]["images"]), 1)
        self.assertTrue(handler.calls[0]["stream"])

    def test_multipart_frames_from_canvas(self):
        handler = FakeHandler(self.cache_dir)
        routes.set_generation_handler(handler)
        png = b"\x89PNG\r\n\x1a\nfake"
        with TestClient(self.app) as client:
            created = client.post(
                "/v1/videos",
                data={"model": "veo-3.1-fast", "prompt": "cat", "seconds": "8", "size": "1280x720", "mode": "frames"},
                files=[
                    ("first_frame", ("first.png", png, "image/png")),
                    ("images", ("first.png", png, "image/png")),
                    ("last_frame", ("last.png", png, "image/png")),
                    ("images", ("last.png", png, "image/png")),
                ],
            )
            self.assertEqual(created.status_code, 200, created.text)
            self._wait_for(client, created.json()["id"], "completed")
        self.assertEqual(handler.calls[0]["model"], "veo_3_1_i2v_s_fast_ultra_fl")
        self.assertEqual(handler.calls[0]["images"], [png, png])

    def test_failure_is_reported(self):
        routes.set_generation_handler(FakeHandler(self.cache_dir, fail=True))
        with TestClient(self.app) as client:
            task = client.post("/v1/videos", json={"model": "omni-1.1-flash", "prompt": "cat"}).json()
            failed = self._wait_for(client, task["id"], "failed")
            self.assertIn("UNUSUAL_ACTIVITY", failed["error"]["message"])
            self.assertEqual(client.get(f"/v1/videos/{task['id']}/content").status_code, 404)

    def test_bad_request_and_unknown_task(self):
        routes.set_generation_handler(FakeHandler(self.cache_dir))
        with TestClient(self.app) as client:
            bad = client.post("/v1/videos", json={"model": "veo-3.1-quality", "prompt": "p", "images": [PNG] * 3})
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(bad.json()["error"]["code"], "unsupported_mode")
            self.assertEqual(client.get("/v1/videos/video_missing").status_code, 404)


if __name__ == "__main__":
    unittest.main()
