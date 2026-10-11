"""Video 1080p/4K upscale (2026-10-11): _poll_video_result passed an undefined
`normalized_tier` to upsample_video, the NameError was swallowed, and callers got the 720p
original with only a stream note. Now: the call carries the account tier, and a failed
upscale is delivered like a failed image enlarge, with flow_upscale = requested vs delivered."""
import json
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.generation_handler import GenerationHandler

UPSAMPLE_1080 = {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}


class VideoUpscaleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = GenerationHandler(
            flow_client=MagicMock(), token_manager=MagicMock(), load_balancer=MagicMock(),
            db=MagicMock(), concurrency_manager=MagicMock(), proxy_manager=None,
        )
        self.handler._update_request_log_progress = AsyncMock()
        self.handler.db.update_task = AsyncMock()
        self.handler.flow_client.check_video_status = AsyncMock(return_value={"operations": [
            {"status": "MEDIA_GENERATION_STATUS_SUCCESSFUL", "operation": {"name": "op-1", "metadata": {}}},
        ]})
        self.handler._resolve_video_asset = AsyncMock(return_value={
            "video_url": "https://cdn.example/v.mp4", "video_media_id": "11111111-2222-3333-4444-555555555555",
            "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT", "media_name": "m-1", "metadata": {},
            "video_info": {}, "model": "veo", "duration": 8,
        })
        self.token = types.SimpleNamespace(
            id=55, at="at", st="st", user_paygate_tier="PAYGATE_TIER_TWO", video_concurrency=1,
        )

    async def _poll(self, stream):
        result = self.handler._create_generation_result()
        state = self.handler._create_response_state()
        chunks = []
        with patch("src.services.generation_handler.config") as cfg, patch("asyncio.sleep", AsyncMock()):
            cfg.max_poll_attempts = 2
            cfg.poll_interval = 0
            cfg.cache_enabled = False
            async for chunk in self.handler._poll_video_result(
                self.token, "proj", [{"operation": {"name": "op-1"}}], stream, UPSAMPLE_1080, result, state, None,
            ):
                chunks.append(chunk)
        return chunks, result, state

    async def test_upscale_call_carries_the_account_tier(self):
        self.handler.flow_client.upsample_video = AsyncMock(return_value={"operations": []})
        await self._poll(stream=False)
        self.handler.flow_client.upsample_video.assert_awaited_once()
        self.assertEqual(self.handler.flow_client.upsample_video.await_args.kwargs["user_paygate_tier"], "PAYGATE_TIER_TWO")

    async def test_failed_upscale_says_requested_and_delivered_non_stream(self):
        self.handler.flow_client.upsample_video = AsyncMock(side_effect=RuntimeError("HTTP Error 400: upsampler refused"))
        chunks, result, state = await self._poll(stream=False)
        body = json.loads(chunks[-1])
        self.assertTrue(result["success"])  # the 720p video is still delivered
        self.assertEqual(body["flow_upscale"]["requested"], "1080p")
        self.assertEqual(body["flow_upscale"]["delivered"], "720p")
        self.assertIn("upsampler refused", body["flow_upscale"]["reason"])
        self.assertEqual(state["generated_assets"]["upscale"]["requested"], "1080p")

    async def test_failed_upscale_says_requested_and_delivered_in_the_final_stream_chunk(self):
        self.handler.flow_client.upsample_video = AsyncMock(return_value={"operations": []})
        chunks, result, state = await self._poll(stream=True)
        final = json.loads(chunks[-1][len("data: "):])
        self.assertEqual(final["flow_upscale"]["requested"], "1080p")
        self.assertEqual(final["flow_upscale"]["delivered"], "720p")
        self.assertIn("<video", final["choices"][0]["delta"]["content"])

    async def test_successful_upscale_reports_the_delivered_size(self):
        async def status(at, ops):
            name = ops[0]["operation"]["name"]
            return {"operations": [{"status": "MEDIA_GENERATION_STATUS_SUCCESSFUL", "operation": {"name": name, "metadata": {}}}]}

        self.handler.flow_client.check_video_status = AsyncMock(side_effect=status)
        self.handler.flow_client.upsample_video = AsyncMock(return_value={"operations": [{"operation": {"name": "op-up"}}]})
        chunks, result, state = await self._poll(stream=False)
        body = json.loads(chunks[-1])
        self.assertTrue(result["success"])
        self.assertEqual(body["flow_upscale"], {"requested": "1080p", "delivered": "1080p"})
        self.handler.flow_client.upsample_video.assert_awaited_once()  # the upscaled video is not upscaled again

    async def test_upscale_job_gets_the_long_wait_not_the_four_minute_cutoff(self):
        polls = {"up": 0}

        async def status(at, ops):
            name = ops[0]["operation"]["name"]
            if name == "op-up":
                polls["up"] += 1
                if polls["up"] < 100:  # ~5 min at 3 s: longer than the normal 80-poll cutoff
                    return {"operations": [{"status": "MEDIA_GENERATION_STATUS_ACTIVE", "operation": {"name": name}}]}
            return {"operations": [{"status": "MEDIA_GENERATION_STATUS_SUCCESSFUL", "operation": {"name": name, "metadata": {}}}]}

        self.handler.flow_client.check_video_status = AsyncMock(side_effect=status)
        self.handler.flow_client.upsample_video = AsyncMock(return_value={"operations": [{"operation": {"name": "op-up"}}]})
        self.handler._fail_video_task = AsyncMock()
        with patch("src.services.generation_handler.config") as cfg, patch("asyncio.sleep", AsyncMock()):
            cfg.max_poll_attempts = 40  # x3 for upscale = 120 polls
            cfg.poll_interval = 0
            cfg.cache_enabled = False
            result = self.handler._create_generation_result()
            chunks = [c async for c in self.handler._poll_video_result(
                self.token, "proj", [{"operation": {"name": "op-1"}}], False, UPSAMPLE_1080, result,
                self.handler._create_response_state(), None,
            )]
        self.assertTrue(result["success"], chunks[-1])
        self.assertEqual(json.loads(chunks[-1])["flow_upscale"], {"requested": "1080p", "delivered": "1080p"})


if __name__ == "__main__":
    unittest.main()
