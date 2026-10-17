from concurrent.futures import ThreadPoolExecutor
import threading
import time
import weakref

from fastapi.testclient import TestClient
import numpy as np
import pytest
import torch
from transformers import AutoModel, AutoProcessor

from media_search.model import Encoder
from media_search.server import create_app
from media_search.store import Store


@pytest.fixture
def model_factory(monkeypatch):
    created = []
    processing = []
    behavior = {"enter": None, "resume": None, "fail": False}

    class Inputs(dict):
        def to(self, device):
            return self

    class Processor:
        def __call__(self, **kwargs):
            return Inputs()

    class Model:
        def eval(self):
            return self

        def to(self, device):
            return self

        def get_text_features(self, **kwargs):
            if behavior["enter"]:
                behavior["enter"].set()
                assert behavior["resume"].wait(3)
            if behavior["fail"]:
                raise ValueError("Inference failed")
            return torch.tensor([[3., 4.]])

        get_image_features = get_text_features

    def processor(*args, **kwargs):
        assert kwargs["local_files_only"] is True
        result = Processor()
        processing.append(weakref.ref(result))
        return result

    def model(*args, **kwargs):
        assert kwargs["local_files_only"] is True
        result = Model()
        created.append(weakref.ref(result))
        return result

    monkeypatch.setattr(AutoProcessor, "from_pretrained", processor)
    monkeypatch.setattr(AutoModel, "from_pretrained", model)
    return created, processing, behavior


def test_idle_release_drops_references_and_next_query_reloads_same_vector(model_factory, monkeypatch):
    created, processing, _ = model_factory
    now = [0.]
    monkeypatch.setattr("media_search.model.time.monotonic", lambda: now[0])
    encoder = Encoder(device="cpu")
    expected = encoder.text("red bicycle")
    now[0] = 299
    assert not encoder.unload_if_idle(300)
    now[0] = 300
    assert encoder.unload_if_idle(300)
    assert not encoder.is_loaded
    assert created[0]() is None and processing[0]() is None
    assert not encoder.unload_if_idle(300)
    np.testing.assert_array_equal(encoder.text("red bicycle"), expected)
    assert encoder.is_loaded and len(created) == 2
    now[0] = 600
    assert encoder.unload_if_idle(300)
    np.testing.assert_array_equal(encoder.images([object()]), [expected])


def test_mps_release_synchronizes_and_clears_allocator_after_dropping_model(model_factory, monkeypatch):
    created, _, _ = model_factory
    calls = []
    monkeypatch.setattr(torch.mps, "synchronize", lambda: calls.append("synchronize"))

    def empty_cache():
        assert created[0]() is None
        calls.append("empty_cache")

    monkeypatch.setattr(torch.mps, "empty_cache", empty_cache)
    encoder = Encoder(device="mps")
    encoder._last_used = time.monotonic() - 301
    assert encoder.unload_if_idle(300)
    assert calls == ["synchronize", "empty_cache"]


def test_active_search_cannot_be_evicted_and_completion_restarts_idle_interval(model_factory):
    _, _, behavior = model_factory
    encoder = Encoder(device="cpu")
    entered, resume, release_done = threading.Event(), threading.Event(), threading.Event()
    behavior.update(enter=entered, resume=resume)
    encoder._last_used = time.monotonic() - 301

    def release():
        result = encoder.unload_if_idle(300)
        release_done.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        query = pool.submit(encoder.text, "red bicycle")
        try:
            assert entered.wait(2)
            cleanup = pool.submit(release)
            assert not release_done.wait(.05)
        finally:
            resume.set()
        np.testing.assert_allclose(query.result(timeout=3), [.6, .8])
        assert cleanup.result(timeout=3) is False
    assert encoder.is_loaded


def test_failed_query_can_be_released_and_a_later_query_recovers(model_factory):
    _, _, behavior = model_factory
    encoder = Encoder(device="cpu")
    behavior["fail"] = True
    with pytest.raises(ValueError, match="Inference failed"):
        encoder.text("red bicycle")
    assert not encoder.unload_if_idle(300)
    encoder._last_used -= 301
    assert encoder.unload_if_idle(300)
    behavior["fail"] = False
    np.testing.assert_allclose(encoder.text("red bicycle"), [.6, .8])


def test_api_health_polling_does_not_hold_model_and_lifespan_stops_cleanup(model_factory, tmp_path, monkeypatch):
    created, _, _ = model_factory
    monkeypatch.setattr("media_search.model.Encoder", lambda: Encoder(device="cpu"))
    before = {t.ident for t in threading.enumerate() if t.name == "findstill-model-idle"}
    with TestClient(create_app(Store(tmp_path), model_idle_seconds=.05)) as client:
        assert client.get("/api/status").json()["query_model_loaded"] is False
        assert not created
        assert client.get("/api/search", params={"q": "red bicycle"}).status_code == 200
        assert len(created) == 1
        deadline = time.monotonic() + 3
        while client.get("/api/status").json()["query_model_loaded"]:
            assert time.monotonic() < deadline, "Status polling kept the model loaded"
            time.sleep(.01)
        assert created[0]() is None
        assert client.get("/api/search", params={"q": "red bicycle"}).status_code == 200
        assert len(created) == 2
    assert {t.ident for t in threading.enumerate() if t.name == "findstill-model-idle"} == before


@pytest.mark.parametrize("idle", [0, -1, float("inf"), float("nan")])
def test_invalid_idle_interval_is_refused(idle, tmp_path):
    with pytest.raises(ValueError, match="finite and positive"):
        create_app(Store(tmp_path), model_idle_seconds=idle)
