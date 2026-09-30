"""Cloudinary storage and AI Vision.

`CloudinaryStore` uploads with the analysis flags Saakshi relies on:
  media_metadata  full EXIF/IPTC/XMP kept alongside the asset
  faces           face boxes, so public outputs blur people automatically
  phash           perceptual hash, used to catch the same photo reused in
                  another project or report
  colors          predominant colours (shown on the asset page)
  quality_analysis focus score, flags blurry evidence

AI Vision (Analyze API) runs in two modes:
  tagging   the NGO taxonomy as tag definitions
  general   a caption for reports, and a before/after change description
            asked on the side-by-side composite image

`LocalStore` is a development fallback that stores files on disk so the app
runs without a Cloudinary account. Transformations and AI Vision are
unavailable in that mode, and the UI says so.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from saakshi_core import taxonomy

from . import transforms

log = logging.getLogger("saakshi.impact.cloud")

CAPTION_PROMPT = (
    "You are writing evidence notes for an NGO impact report. In one or two plain sentences, describe what this "
    "field photo shows: the setting, visible activity, and the condition of the site. Do not guess names or places."
)
CHANGE_PROMPT = (
    "This image is two photos of the same site side by side: the LEFT half was taken BEFORE, the RIGHT half AFTER. "
    "In two plain sentences, describe the visible changes between before and after (for example waste removed, "
    "plants grown, water cleaner, structures built or damaged). If you cannot see a change, say so."
)


# Cloudinary AI Vision tagging mode rejects requests with more tag definitions than this
MAX_TAG_DEFINITIONS = 10


class CloudError(RuntimeError):
    pass


class CloudinaryStore:
    kind = "cloudinary"

    def __init__(self, cloud_name: str, api_key: str, api_secret: str, folder: str = "saakshi", ai_vision: bool = True):
        import cloudinary

        cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret, secure=True)
        self.cloud_name = cloud_name
        self.api_key = api_key
        self.api_secret = api_secret
        self.folder = folder.strip("/")
        self.ai_vision_enabled = ai_vision
        self.ai_vision_error: Optional[str] = None
        self.last_quota: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ upload
    def upload(self, data: bytes, public_id: str, asset_folder: str, tags: List[str], context: Dict[str, str],
               resource_type: str = "image") -> Dict[str, Any]:
        import cloudinary.uploader

        if resource_type == "video":
            try:
                import io

                return cloudinary.uploader.upload_large(
                    io.BytesIO(data), filename=f"{public_id.split('/')[-1]}.mp4", public_id=public_id, asset_folder=asset_folder, tags=tags, context=context,
                    overwrite=False, unique_filename=False, resource_type="video", media_metadata=True,
                )
            except Exception as exc:
                raise CloudError(f"Cloudinary video upload failed: {exc}") from exc
        base = dict(
            public_id=public_id,
            asset_folder=asset_folder,
            tags=tags,
            context=context,
            overwrite=False,
            unique_filename=False,
            resource_type="image",
            media_metadata=True,
            faces=True,
            phash=True,
            colors=True,
        )
        try:
            return cloudinary.uploader.upload(data, quality_analysis=True, **base)
        except Exception as exc:
            # Some plans reject quality_analysis; retry without it rather than fail the ingest
            if "quality" in str(exc).lower():
                log.info("quality_analysis not available on this plan; uploading without it")
                return cloudinary.uploader.upload(data, **base)
            raise CloudError(f"Cloudinary upload failed: {exc}") from exc

    def update(self, public_id: str, context: Optional[Dict[str, str]] = None, tags: Optional[List[str]] = None,
               resource_type: str = "image") -> None:
        import cloudinary.uploader

        try:
            if context:
                cloudinary.uploader.add_context(context, [public_id], resource_type=resource_type)
            if tags:
                cloudinary.uploader.add_tag(",".join(tags), [public_id], resource_type=resource_type)
        except Exception as exc:
            log.warning("Could not update Cloudinary metadata for %s: %s", public_id, exc)

    def search_all(self, expression: str, max_results: int = 500) -> List[Dict[str, Any]]:
        import cloudinary

        out: List[Dict[str, Any]] = []
        cursor = None
        while True:
            q = cloudinary.Search().expression(expression).with_field("context").with_field("tags").max_results(min(500, max_results))
            if cursor:
                q = q.next_cursor(cursor)
            res = q.execute()
            out.extend(res.get("resources", []))
            cursor = res.get("next_cursor")
            if not cursor or len(out) >= max_results:
                break
        return out

    # ------------------------------------------------------------- AI Vision
    def _analyze(self, mode: str, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"https://api.cloudinary.com/v2/analysis/{self.cloud_name}/analyze/{mode}"
        r = httpx.post(url, json=body, auth=(self.api_key, self.api_secret), timeout=90.0)
        if r.status_code >= 400:
            raise CloudError(f"AI Vision {mode} returned {r.status_code}: {r.text[:300]}")
        data = r.json()
        quota = (data.get("limits") or {}).get("addons_quota")
        if quota:
            self.last_quota = quota[0]
        return data

    def ai_tags(self, image_url: str) -> List[str]:
        """Taxonomy tags for one image. The tagging analyzer takes at most
        MAX_TAG_DEFINITIONS per request, so the taxonomy goes in even batches."""
        defs = taxonomy.ai_vision_definitions()
        size = -(-len(defs) // -(-len(defs) // MAX_TAG_DEFINITIONS))
        batches = [defs[i:i + size] for i in range(0, len(defs), size)]
        found = set()
        with ThreadPoolExecutor(max_workers=len(batches)) as pool:  # batches are independent requests
            for data in pool.map(lambda b: self._analyze("ai_vision_tagging", {"source": {"uri": image_url}, "tag_definitions": b}), batches):
                found.update(taxonomy.from_ai_vision_name(t.get("name")) for t in ((data.get("data") or {}).get("analysis") or {}).get("tags") or [])
        return [name for name in taxonomy.TAG_NAMES if name in found]

    def ai_answer(self, image_url: str, prompts: List[str]) -> List[str]:
        data = self._analyze("ai_vision_general", {"source": {"uri": image_url}, "prompts": prompts})
        responses = ((data.get("data") or {}).get("analysis") or {}).get("responses") or []
        return [str(r.get("value", "")).strip() for r in responses]

    # --------------------------------------------------------------- delivery
    def url_original(self, public_id: str) -> str:
        return transforms.build(public_id, [{"fetch_format": "auto", "quality": "auto"}])[0]

    def url_thumb(self, public_id: str, blur_faces: bool = False) -> str:
        return transforms.thumb(public_id, blur_faces=blur_faces)

    def url_display(self, public_id: str, blur_faces: bool = False) -> str:
        return transforms.display(public_id, blur_faces=blur_faces)

    @property
    def transformations(self) -> bool:
        return True


class LocalStore:
    """Development fallback: files on disk, no transformations, no AI Vision."""

    kind = "local"
    ai_vision_enabled = False
    ai_vision_error = "Cloudinary is not configured"
    last_quota = None
    cloud_name = None

    def __init__(self, root: Path, public_base: str = ""):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base = public_base.rstrip("/")

    def _path(self, public_id: str) -> Path:
        return self.root / (public_id.replace("/", "__") + ".jpg")

    def upload(self, data: bytes, public_id: str, asset_folder: str, tags: List[str], context: Dict[str, str],
               resource_type: str = "image") -> Dict[str, Any]:
        if resource_type == "video":
            raise CloudError("Video evidence needs Cloudinary; configure CLOUDINARY_URL")
        p = self._path(public_id)
        p.write_bytes(data)
        from saakshi_core.imaging import dhash, open_image

        img = open_image(data)
        return {
            "public_id": public_id,
            "version": int(time.time()),
            "secure_url": self.url_original(public_id),
            "format": "jpg",
            "width": img.width,
            "height": img.height,
            "bytes": len(data),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "phash": dhash(img),
            "faces": [],
            "asset_folder": asset_folder,
        }

    def update(self, public_id: str, context: Optional[Dict[str, str]] = None, tags: Optional[List[str]] = None,
               resource_type: str = "image") -> None:
        return None

    def search_all(self, expression: str, max_results: int = 500) -> List[Dict[str, Any]]:
        return []

    def ai_tags(self, image_url: str) -> List[str]:
        raise CloudError("AI Vision needs Cloudinary")

    def ai_answer(self, image_url: str, prompts: List[str]) -> List[str]:
        raise CloudError("AI Vision needs Cloudinary")

    def url_original(self, public_id: str) -> str:
        return f"{self.public_base}/media/{public_id.replace('/', '__')}.jpg"

    url_thumb = lambda self, public_id, blur_faces=False: self.url_original(public_id)  # noqa: E731
    url_display = lambda self, public_id, blur_faces=False: self.url_original(public_id)  # noqa: E731

    @property
    def transformations(self) -> bool:
        return False

    def file_for(self, name: str) -> Optional[Path]:
        p = self.root / name
        return p if p.exists() else None
