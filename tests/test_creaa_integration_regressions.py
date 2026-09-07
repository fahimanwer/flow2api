"""Integration boundaries discovered while wiring the real website catalog."""
import json
from pathlib import Path
import pytest
from src.core.creaa_models import normalize_catalog, normalize_generation_request
from src.services.creaa_bridge import CreaaBridge
from test_creaa_bridge import FakeSocket, register, submit_and_dispatch, send_event


def test_real_website_catalog_accepts_namespaced_gpt_model():
    source = json.loads((Path(__file__).parent / 'fixtures/creaa/models.json').read_text())
    models = [{"id": item["model_id"], "media_type": kind, "label": item["display_name"], "params": item["params"]}
              for kind in ('image', 'video') for item in source[kind + '_models']]
    normalized = normalize_catalog(models)
    assert any(m['id'] == 'openai/gpt-image-2' for m in normalized)
    req = normalize_generation_request('image', {'model':'creaa/openai/gpt-image-2','prompt':'A cup'})
    assert req['model'] == 'openai/gpt-image-2'


def test_video_accepts_catalog_dimensions_and_minimax_2k():
    for resolution in ['1280x720','720x1280','2K']:
        req = normalize_generation_request('video', {'model':'seedance-2.5','prompt':'Camera move','resolution':resolution})
        assert req['resolution'].lower() == resolution.lower()


@pytest.mark.asyncio
async def test_ack_send_error_after_bytes_sent_never_requeues_generation(tmp_path):
    bridge = CreaaBridge(tmp_path/'creaa.db',housekeeping_interval=0)
    await bridge.start()
    socket = await register(bridge,'dev1','account1')
    job = await submit_and_dispatch(bridge,socket)
    original_send = socket.send_text
    async def send_then_error(text):
        await original_send(text)  # client may receive the ack before server sees an exception
        raise ConnectionError('transport closed after sending bytes')
    socket.send_text = send_then_error
    await send_event(bridge,socket,job['id'],job['attempt_id'],'submitting')
    assert bridge.get_job(job['id'])['state'] == 'needs_review'
    await bridge.close()
