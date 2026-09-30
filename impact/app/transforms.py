"""Cloudinary delivery URLs for every derived asset, plus a reader that turns a
transformation string back into plain-language steps (the provenance panel).

Nothing here calls the network: Cloudinary renders a transformation the first
time its URL is requested, so building the URL is enough.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import cloudinary
from cloudinary.utils import cloudinary_url

# Social formats for the campaign pack
FORMATS: List[Dict[str, Any]] = [
    {"key": "square", "name": "Instagram / Facebook post", "w": 1080, "h": 1080},
    {"key": "portrait", "name": "Instagram portrait", "w": 1080, "h": 1350},
    {"key": "story", "name": "Story / WhatsApp status", "w": 1080, "h": 1920},
    {"key": "landscape", "name": "Link preview / X / LinkedIn", "w": 1200, "h": 630},
]

BRAND_BG = "rgb:0E3B2E"


def layer_id(public_id: str) -> str:
    """Overlay ids use ':' instead of '/' for folders."""
    return public_id.replace("/", ":")


def _safe_text(text: str, limit: int = 140) -> str:
    text = (text or "").replace("\n", " ").strip()
    # commas and slashes have meaning inside l_text; replace with look-alikes
    text = text.replace(",", "‚").replace("/", "∕")
    return text[:limit]


def build(public_id: str, transformation: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Return (url, transformation_string)."""
    url, _ = cloudinary_url(public_id, transformation=transformation, secure=True)
    m = re.search(r"/image/upload/(.+?)/v\d+/", url) or re.search(r"/image/upload/(.+)/[^/]+$", url)
    tstring = m.group(1) if m else ""
    return url, tstring


def video_frame(public_id: str, w: int = 480, h: int = 360, blur_faces: bool = False) -> str:
    """A still from the video (Cloudinary picks a representative frame with so_auto)."""
    t: List[Dict[str, Any]] = [{"start_offset": "auto"}, {"width": w, "height": h, "crop": "fill", "gravity": "auto"}]
    if blur_faces:
        t.append({"effect": "blur_faces:800"})
    t.append({"quality": "auto"})
    url, _ = cloudinary_url(public_id, resource_type="video", format="jpg", transformation=t, secure=True)
    return url


def video_url(public_id: str, w: int = 1280) -> str:
    url, _ = cloudinary_url(public_id, resource_type="video", transformation=[{"width": w, "crop": "limit"}, {"fetch_format": "auto", "quality": "auto"}], secure=True)
    return url


def thumb(public_id: str, w: int = 480, h: int = 360, blur_faces: bool = False) -> str:
    t: List[Dict[str, Any]] = [{"width": w, "height": h, "crop": "fill", "gravity": "auto"}]
    if blur_faces:
        t.append({"effect": "blur_faces:800"})
    t.append({"fetch_format": "auto", "quality": "auto"})
    return build(public_id, t)[0]


def display(public_id: str, w: int = 1600, blur_faces: bool = False) -> str:
    t: List[Dict[str, Any]] = [{"width": w, "crop": "limit"}]
    if blur_faces:
        t.append({"effect": "blur_faces:800"})
    t.append({"fetch_format": "auto", "quality": "auto"})
    return build(public_id, t)[0]


def _label(text: str, size: int, gravity: str, x: int, y: int) -> List[Dict[str, Any]]:
    return [
        {"overlay": {"font_family": "Arial", "font_size": size, "font_weight": "bold", "text": _safe_text(text, 60)},
         "color": "white", "background": BRAND_BG, "border": f"{max(6, size // 3)}px_solid_{BRAND_BG}"},
        {"flags": "layer_apply", "gravity": gravity, "x": x, "y": y},
    ]


def before_after(before_id: str, after_id: str, before_label: str, after_label: str,
                 w: int = 800, h: int = 600, blur_faces: bool = True) -> Tuple[str, str]:
    """Side-by-side composite: before on the left, after on the right, one image URL."""
    t: List[Dict[str, Any]] = [
        {"width": w, "height": h, "crop": "fill", "gravity": "auto"},
        {"width": w * 2, "height": h, "crop": "pad", "gravity": "west", "background": "white"},
        {"overlay": layer_id(after_id), "width": w, "height": h, "crop": "fill", "gravity": "auto"},
        {"flags": "layer_apply", "gravity": "east"},
    ]
    if blur_faces:
        t.append({"effect": "blur_faces:800"})
    size = max(22, w // 22)
    t += _label(before_label, size, "north_west", 18, 18)
    t += _label(after_label, size, "north_east", 18, 18)
    t.append({"fetch_format": "auto", "quality": "auto"})
    return build(before_id, t)


def before_after_stacked(before_id: str, after_id: str, before_label: str, after_label: str,
                         w: int = 1080, blur_faces: bool = True) -> Tuple[str, str]:
    """Square social composite: before on top, after below."""
    half = w // 2
    t: List[Dict[str, Any]] = [
        {"width": w, "height": half, "crop": "fill", "gravity": "auto"},
        {"width": w, "height": w, "crop": "pad", "gravity": "north", "background": "white"},
        {"overlay": layer_id(after_id), "width": w, "height": half, "crop": "fill", "gravity": "auto"},
        {"flags": "layer_apply", "gravity": "south"},
    ]
    if blur_faces:
        t.append({"effect": "blur_faces:800"})
    size = max(26, w // 24)
    t += _label(before_label, size, "north_west", 24, 24)
    t += _label(after_label, size, "south_west", 24, 24)
    t.append({"fetch_format": "auto", "quality": "auto"})
    return build(before_id, t)


def campaign_variant(public_id: str, fmt: Dict[str, Any], caption: str, credit: str, blur_faces: bool = True) -> Tuple[str, str]:
    w, h = fmt["w"], fmt["h"]
    t: List[Dict[str, Any]] = [
        {"width": w, "height": h, "crop": "fill", "gravity": "auto"},
        {"effect": "improve:outdoor"},
    ]
    if blur_faces:
        t.append({"effect": "blur_faces:800"})
    if caption:
        size = max(30, w // 26)
        t += [
            {"overlay": {"font_family": "Arial", "font_size": size, "font_weight": "bold", "text": _safe_text(caption)},
             "color": "white", "background": BRAND_BG, "border": f"{size // 2}px_solid_{BRAND_BG}",
             "width": int(w * 0.86), "crop": "fit"},
            {"flags": "layer_apply", "gravity": "south", "y": int(h * 0.05)},
        ]
    if credit:
        t += [
            {"overlay": {"font_family": "Arial", "font_size": max(18, w // 48), "text": _safe_text(credit, 80)},
             "color": "white", "background": "rgb:00000099", "border": "8px_solid_rgb:00000099"},
            {"flags": "layer_apply", "gravity": "north_east", "x": 20, "y": 20},
        ]
    t.append({"fetch_format": "auto", "quality": "auto"})
    return build(public_id, t)


def analysis_jpg(url: str) -> str:
    """The same delivery URL forced to JPEG, for AI Vision (f_auto may negotiate AVIF/WebP)."""
    import re as _re

    out = _re.sub(r"(^|/|,)f_auto(?=,|/)", lambda m: m.group(1), url)
    out = out.replace(",/", "/").replace("/,", "/").replace("//v", "/v")
    tail = out.rsplit("/", 1)[-1]
    if "." not in tail:
        out += ".jpg"
    return out


def attachment(url: str, filename: str) -> str:
    """Same URL, but the browser downloads it (fl_attachment)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", filename)[:60] or "saakshi"
    return url.replace("/image/upload/", f"/image/upload/fl_attachment:{safe}/", 1)


# ------------------------------------------------------------------- provenance
_GRAVITY = {
    "auto": "automatic subject focus", "west": "left", "east": "right", "north": "top", "south": "bottom",
    "north_west": "top left", "north_east": "top right", "south_west": "bottom left", "south_east": "bottom right",
    "center": "centre", "face": "face", "faces": "faces",
}


def explain(transformation: str) -> List[str]:
    """Turn 'c_fill,g_auto,h_450,w_600/e_blur_faces:800/...' into readable steps."""
    steps: List[str] = []
    for comp in [c for c in transformation.split("/") if c]:
        params = {}
        for part in comp.split(","):
            if "_" in part:
                k, v = part.split("_", 1)
                params[k] = v
        text = _explain_component(params)
        if text:
            steps.append(text)
    return steps


def _explain_component(p: Dict[str, str]) -> Optional[str]:
    from urllib.parse import unquote

    if "l" in p:
        v = p["l"]
        if v.startswith("text:"):
            body = unquote(v.split(":", 2)[-1]).replace("‚", ",").replace("∕", "/")
            return f'Add text "{body}"'
        size = f" at {p.get('w', '?')}×{p.get('h', '?')}" if "w" in p else ""
        return f"Overlay photo {v.replace(':', '/')}{size}"
    if p.get("fl") == "layer_apply":
        where = _GRAVITY.get(p.get("g", ""), p.get("g", "centre"))
        return f"Place the layer at the {where}"
    if "e" in p:
        eff = p["e"]
        if eff.startswith("blur_faces"):
            return "Blur every detected face (privacy)"
        if eff.startswith("pixelate_faces"):
            return "Pixelate every detected face (privacy)"
        if eff.startswith("improve"):
            return "Auto-improve colour and contrast"
        return f"Apply effect {eff}"
    if "c" in p:
        crop = {"fill": "Crop to fill", "pad": "Pad the canvas to", "limit": "Resize to fit within", "fit": "Fit within", "scale": "Scale to"}.get(p["c"], f"Crop ({p['c']})")
        size = "×".join(x for x in (p.get("w"), p.get("h")) if x)
        g = p.get("g")
        extra = f", {_GRAVITY.get(g, g)}" if g else ""
        return f"{crop} {size}{extra}".strip()
    if p.get("f") == "auto" or p.get("q") == "auto":
        return "Deliver in the best format and quality for the viewer's device"
    if "w" in p or "h" in p:
        return "Resize to " + "×".join(x for x in (p.get("w"), p.get("h")) if x)
    return None


def configured() -> bool:
    cfg = cloudinary.config()
    return bool(cfg.cloud_name and cfg.api_key and cfg.api_secret)
