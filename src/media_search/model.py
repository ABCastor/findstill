"""Pinned SigLIP2 image and text encoders. Scores are cosine similarities, not probabilities."""
import os
import threading
import numpy as np
from .config import MODEL, REVISION


class Encoder:
    def __init__(self, device=None, allow_download=False):
        import torch
        from transformers import AutoModel, AutoProcessor
        torch.set_num_threads(4)
        self.device = device or os.environ.get("MEDIA_SEARCH_DEVICE") or ("mps" if torch.backends.mps.is_available() else "cpu")
        self.lock = threading.Lock()
        self.processor = AutoProcessor.from_pretrained(MODEL, revision=REVISION, trust_remote_code=False, use_fast=False, local_files_only=not allow_download)
        self.model = AutoModel.from_pretrained(MODEL, revision=REVISION, trust_remote_code=False, local_files_only=not allow_download).eval().to(self.device)

    @staticmethod
    def normalize(features):
        vectors = features.float().cpu().numpy().astype(np.float32)
        return vectors / np.maximum(np.linalg.norm(vectors, axis=-1, keepdims=True), 1e-12)

    def images(self, images):
        import torch
        with self.lock, torch.inference_mode():
            inputs = self.processor(images=images, return_tensors="pt").to(self.device)
            return self.normalize(self.model.get_image_features(**inputs))

    def text(self, query):
        import torch
        with self.lock, torch.inference_mode():
            inputs = self.processor(text=[query], padding="max_length", max_length=64, truncation=True, return_tensors="pt").to(self.device)
            return self.normalize(self.model.get_text_features(**inputs))[0]
