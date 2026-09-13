"""Authoritative story and audio-form policies.

Story content comes from each series bible. This module only defines the
audible form shared by generation, story approval, and audio compilation.
"""
from __future__ import annotations


STORY_POLICIES = {
    "family": {
        "dialogue_min_segments": 1,
        "dialogue_max_segments": 10,
        "dialogue_max_chars": 18,
        "narrator_min_ratio": 0.65,
        "narrator_max_ratio": 0.85,
        "max_consecutive_dialogue": 1,
        "narrator_segment_max_chars": 90,
    },
    "imagination": {
        "dialogue_min_segments": 1,
        "dialogue_max_segments": 4,
        "dialogue_max_chars": 24,
        "narrator_min_ratio": 0.70,
        "narrator_max_ratio": 0.90,
        "max_consecutive_dialogue": 1,
        "narrator_segment_max_chars": 90,
    },
}


def story_policy(story_type: str) -> dict:
    canonical = "imagination" if story_type in {"fantasy", "imagination"} else "family"
    return dict(STORY_POLICIES[canonical])


def audio_policy(story_type: str) -> dict:
    policy = story_policy(story_type)
    return {
        key: policy[key]
        for key in (
            "narrator_min_ratio",
            "narrator_max_ratio",
            "max_consecutive_dialogue",
            "dialogue_max_chars",
            "narrator_segment_max_chars",
        )
    }
