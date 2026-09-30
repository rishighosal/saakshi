"""Evidence taxonomy for NGO field work.

Each tag is used three ways:
  * `question` feeds Cloudinary AI Vision tagging mode (tag_definitions).
  * `prompt` feeds CLIP zero-shot tagging on the device (works offline).
  * `keywords` help keyword search and before/after reasoning.
"""

from __future__ import annotations

from typing import Dict, List

TAGS: List[Dict[str, object]] = [
    {
        "name": "sapling_planting",
        "question": "Does the image show young saplings or seedlings that were recently planted?",
        "prompt": "a photo of young saplings recently planted in soil",
        "keywords": ["sapling", "seedling", "plantation", "planting", "tree"],
    },
    {
        "name": "mangrove",
        "question": "Does the image show mangrove trees or a mangrove plantation in coastal mud or tidal water?",
        "prompt": "a photo of mangrove trees in coastal mud",
        "keywords": ["mangrove", "sundarbans", "tidal", "creek"],
    },
    {
        "name": "tree_cover",
        "question": "Does the image show healthy trees or dense green vegetation?",
        "prompt": "a photo of healthy green trees and vegetation",
        "keywords": ["green", "canopy", "vegetation", "trees"],
    },
    {
        "name": "waste_present",
        "question": "Is there visible litter, garbage piles or plastic waste on the ground or in water?",
        "prompt": "a photo of garbage and plastic litter on the ground",
        "keywords": ["garbage", "litter", "waste", "plastic", "trash", "dump"],
    },
    {
        "name": "clean_area",
        "question": "Is this an outdoor area that looks clean with no visible litter?",
        "prompt": "a photo of a clean street with no litter",
        "keywords": ["clean", "cleaned", "tidy", "cleanup"],
    },
    {
        "name": "water_body",
        "question": "Does the image show a pond, lake, river or canal?",
        "prompt": "a photo of a pond or lake",
        "keywords": ["pond", "lake", "river", "canal", "water"],
    },
    {
        "name": "flooding",
        "question": "Does the image show flooding, a waterlogged road or standing flood water?",
        "prompt": "a photo of a flooded road with standing water",
        "keywords": ["flood", "flooded", "waterlogged", "inundated"],
    },
    {
        "name": "construction",
        "question": "Does the image show construction work, building materials or infrastructure being built?",
        "prompt": "a photo of construction work with bricks and building materials",
        "keywords": ["construction", "building", "bricks", "cement", "infrastructure"],
    },
    {
        "name": "sanitation",
        "question": "Does the image show toilets, handwashing stations or other sanitation facilities?",
        "prompt": "a photo of a public toilet or handwashing station",
        "keywords": ["toilet", "sanitation", "handwash", "latrine"],
    },
    {
        "name": "community_event",
        "question": "Does the image show a group of people gathered for a community activity, meeting or training?",
        "prompt": "a photo of a group of villagers at a community meeting",
        "keywords": ["community", "meeting", "training", "volunteers", "people", "gathering"],
    },
    {
        "name": "damage",
        "question": "Does the image show damage such as erosion, fallen trees, broken structures or destruction?",
        "prompt": "a photo of damaged infrastructure and erosion after a storm",
        "keywords": ["damage", "damaged", "erosion", "broken", "collapsed", "storm", "cyclone"],
    },
    {
        "name": "solar_energy",
        "question": "Does the image show solar panels or other renewable energy equipment?",
        "prompt": "a photo of solar panels",
        "keywords": ["solar", "panel", "renewable"],
    },
]

TAG_NAMES = [t["name"] for t in TAGS]

# Tags that indicate a problem; an "after" photo losing them is a positive change.
PROBLEM_TAGS = {"waste_present", "flooding", "damage"}
# Tags that indicate progress; an "after" photo gaining them is a positive change.
PROGRESS_TAGS = {"sapling_planting", "mangrove", "tree_cover", "clean_area", "sanitation", "solar_energy", "construction"}

# Words in a field note that make an item urgent for sync
URGENT_WORDS = ["flood", "damage", "damaged", "injury", "collapsed", "urgent", "emergency", "breach", "cyclone", "fire"]


def ai_vision_definitions() -> List[Dict[str, str]]:
    """tag_definitions payload for Cloudinary AI Vision tagging mode.

    AI Vision tag names allow only lower-case letters, digits and hyphens;
    `from_ai_vision_name` maps them back."""
    return [{"name": str(t["name"]).replace("_", "-"), "description": str(t["question"])} for t in TAGS]


def from_ai_vision_name(name: str) -> str:
    return (name or "").replace("-", "_")


def label(tag: str) -> str:
    return tag.replace("_", " ").capitalize()
