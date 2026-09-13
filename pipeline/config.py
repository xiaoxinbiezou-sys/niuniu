"""全局配置加载。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("project", {})
    cfg.setdefault("tts", {})
    cfg.setdefault("image", {})
    cfg.setdefault("animation", {})
    cfg.setdefault("subtitle", {})
    cfg.setdefault("compose", {})
    return cfg
