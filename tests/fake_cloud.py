"""A stand-in for CloudinaryStore used in tests (no network).

It behaves like the real store: returns a phash, faces, quality score, builds
real Cloudinary transformation URLs, and answers AI Vision calls with tags
guessed from the file name.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import cloudinary

from impact.app import transforms
from saakshi_core.imaging import dhash, open_image


class FakeCloudinary:
    kind = "cloudinary"
    cloud_name = "demo-cloud"

    def __init__(self):
        cloudinary.config(cloud_name=self.cloud_name, api_key="k", api_secret="s", secure=True)
        self.ai_vision_enabled = True
        self.ai_vision_error = None
        self.last_quota = {"type": "ai_vision", "remaining": 950, "limit": 1000}
        self.uploads: Dict[str, Dict[str, Any]] = {}
        self.names: Dict[str, str] = {}
        self.context: Dict[str, Dict[str, str]] = {}
        self.calls: List[str] = []

    def upload(self, data: bytes, public_id: str, asset_folder: str, tags: List[str], context: Dict[str, str],
               resource_type: str = "image") -> Dict[str, Any]:
        if resource_type == "video":
            res = {"public_id": public_id, "version": int(time.time()), "secure_url": f"https://res.cloudinary.com/{self.cloud_name}/video/upload/v1/{public_id}.mp4",
                   "format": "mp4", "width": 1280, "height": 720, "bytes": len(data), "duration": 12.5,
                   "media_metadata": {"GPSCoordinates": "22 deg 33' 10.80\" N, 88 deg 21' 7.20\" E, 0 m", "CreateDate": "2026:09:27 09:00:00"}}
            self.uploads[public_id] = res
            self.context[public_id] = dict(context)
            return res
        img = open_image(data)
        res = {
            "public_id": public_id, "version": int(time.time()), "asset_id": "a" * 32,
            "secure_url": f"https://res.cloudinary.com/{self.cloud_name}/image/upload/v1/{public_id}.jpg",
            "format": "jpg", "width": img.width, "height": img.height, "bytes": len(data),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "phash": dhash(img),
            "faces": [], "colors": [["#7A6A55", 40.1], ["#B0C6D6", 20.3]], "quality_analysis": {"focus": 0.82},
            "asset_folder": asset_folder, "media_metadata": {"Make": "Saakshi"},
        }
        self.uploads[public_id] = res
        self.context[public_id] = dict(context)
        return res

    def update(self, public_id: str, context: Optional[Dict[str, str]] = None, tags: Optional[List[str]] = None,
               resource_type: str = "image") -> None:
        self.context.setdefault(public_id, {}).update(context or {})

    def search_all(self, expression: str, max_results: int = 500) -> List[Dict[str, Any]]:
        return []

    def ai_tags(self, image_url: str) -> List[str]:
        self.calls.append("tagging")
        name = image_url.lower()
        # the test sets which tags each upload gets through `names`
        for key, tags in self.names.items():
            if key in name:
                return tags
        return ["clean_area"]

    def ai_answer(self, image_url: str, prompts: List[str]) -> List[str]:
        self.calls.append("general")
        if "LEFT" in prompts[0]:
            return ["The litter visible before has been removed and the ground is now clear."]
        return ["A street corner in a residential area."]

    def url_original(self, public_id: str) -> str:
        return transforms.build(public_id, [{"fetch_format": "auto", "quality": "auto"}])[0]

    def url_thumb(self, public_id: str, blur_faces: bool = False) -> str:
        return transforms.thumb(public_id, blur_faces=blur_faces)

    def url_display(self, public_id: str, blur_faces: bool = False) -> str:
        return transforms.display(public_id, blur_faces=blur_faces)

    @property
    def transformations(self) -> bool:
        return True
