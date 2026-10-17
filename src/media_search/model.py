"""Pinned SigLIP2 image and text encoders. Scores are cosine similarities, not probabilities."""
import os
import gc
import threading
import time
import numpy as np
from .config import MODEL, REVISION


class Encoder:
    def __init__(self, device=None, allow_download=False):
        import torch
        torch.set_num_threads(4)
        self.device = device or os.environ.get("MEDIA_SEARCH_DEVICE") or ("mps" if torch.backends.mps.is_available() else "cpu")
        self.lock = threading.Lock()
        self.allow_download = allow_download
        self.model = self.processor = None
        self._last_used = time.monotonic()
        self._load()

    def _load(self):
        from transformers import AutoModel, AutoProcessor
        processor = AutoProcessor.from_pretrained(MODEL, revision=REVISION, trust_remote_code=False, use_fast=False, local_files_only=not self.allow_download)
        model = AutoModel.from_pretrained(MODEL, revision=REVISION, trust_remote_code=False, local_files_only=not self.allow_download).eval().to(self.device)
        self.processor, self.model = processor, model

    @property
    def is_loaded(self):
        return self.model is not None

    def unload_if_idle(self, idle_seconds):
        # The same lock protects inference, reload and release. A long search is
        # never idle; its completion starts a fresh idle interval, even on error.
        with self.lock:
            if self.model is None or time.monotonic() - self._last_used < idle_seconds:
                return False
            import torch
            if str(self.device).startswith("mps"):
                torch.mps.synchronize()
            self.model = self.processor = None
            gc.collect()
            if str(self.device).startswith("mps"):
                torch.mps.empty_cache()
            return True

    @staticmethod
    def normalize(features):
        vectors = features.float().cpu().numpy().astype(np.float32)
        return vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-12)

    def images(self, images):
        import torch
        with self.lock, torch.inference_mode():
            try:
                if self.model is None:
                    self._load()
                inputs = self.processor(images=images, return_tensors="pt").to(self.device)
                return self.normalize(self.model.get_image_features(**inputs))
            finally:
                self._last_used = time.monotonic()

    def text(self, query):
        import torch
        with self.lock, torch.inference_mode():
            try:
                if self.model is None:
                    self._load()
                inputs = self.processor(text=[query], padding="max_length", max_length=64, truncation=True, return_tensors="pt").to(self.device)
                return self.normalize(self.model.get_text_features(**inputs))[0]
            finally:
                self._last_used = time.monotonic()
