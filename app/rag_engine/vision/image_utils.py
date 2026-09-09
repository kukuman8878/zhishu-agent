"""图像处理工具：加载 / 缩放 / 裁剪 / 放大 / 编码 / 高 DPI 区域重渲染（P2）。

- 区域精读优先从原始 PDF 按 REGION_DPI 重新渲染（无插值损失）；
- 元素裁剪图（MinerU 输出）用 LANCZOS 放大；
- 所有函数返回 PIL Image，编码统一走 pil_to_data_url。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from PIL import Image

from ..config import settings

BBox = Tuple[float, float, float, float]  # (x0, y0, x1, y1) 归一化 0~1000


def load_image(path: str | Path) -> Optional[Image.Image]:
    try:
        return Image.open(path).convert("RGB")
    except (OSError, ValueError):
        return None


def smart_resize(img: Image.Image, max_dim: int = 0) -> Image.Image:
    """等比例缩小到 max_dim 以内（不放大）。"""
    max_dim = max_dim or settings.full_image_max_dim
    w, h = img.size
    m = max(w, h)
    if m <= max_dim:
        return img
    r = max_dim / m
    return img.resize((max(1, int(w * r)), max(1, int(h * r))), Image.LANCZOS)


def crop_region(img: Image.Image, bbox: BBox, pad_frac: float = 0.02) -> Image.Image:
    """按归一化 bbox 裁剪，带少量外扩。"""
    w, h = img.size
    x0 = max(0, int(bbox[0] / 1000 * w))
    y0 = max(0, int(bbox[1] / 1000 * h))
    x1 = min(w, int(bbox[2] / 1000 * w))
    y1 = min(h, int(bbox[3] / 1000 * h))
    px = int((x1 - x0) * pad_frac)
    py = int((y1 - y0) * pad_frac)
    x0, y0 = max(0, x0 - px), max(0, y0 - py)
    x1, y1 = min(w, x1 + px), min(h, y1 + py)
    if x1 <= x0 or y1 <= y0:
        return img
    return img.crop((x0, y0, x1, y1))


def upscale(img: Image.Image, factor: int = 0) -> Image.Image:
    """LANCZOS 放大 factor 倍（默认 settings.crop_upscale）。"""
    factor = factor or settings.crop_upscale
    w, h = img.size
    return img.resize((w * factor, h * factor), Image.LANCZOS)


def pil_to_data_url(img: Image.Image, quality: int = 90) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def pil_message(img: Image.Image, label: str = "", quality: int = 90) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": pil_to_data_url(img, quality=quality)},
    }


def save_jpeg(img: Image.Image, path: str | Path, quality: int = 90) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(str(p), format="JPEG", quality=quality)
    return str(p)


def render_pdf_region(
    pdf_path: str | Path,
    page_idx: int,
    bbox: Optional[BBox] = None,
    dpi: Optional[int] = None,
) -> Optional[Image.Image]:
    """从原始 PDF 以高 DPI 重渲染整页或区域（比放大 JPEG 更清晰）。"""
    import pymupdf

    dpi = dpi or settings.region_dpi
    zoom = dpi / 72.0
    try:
        with pymupdf.open(str(pdf_path)) as pdf:
            if page_idx < 0 or page_idx >= len(pdf):
                return None
            page = pdf[page_idx]
            rect = page.rect
            clip = None
            if bbox:
                clip = pymupdf.Rect(
                    bbox[0] / 1000 * rect.width,
                    bbox[1] / 1000 * rect.height,
                    bbox[2] / 1000 * rect.width,
                    bbox[3] / 1000 * rect.height,
                )
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
            return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    except Exception:
        return None


def image_density(img: Image.Image) -> float:
    """边缘/纹理密度启发值：工程图、密集小字图显著偏高（供视觉路由判题用）。"""
    g = img.convert("L").resize((512, 512), Image.BILINEAR)
    arr = np.asarray(g, dtype=np.float32)
    gy, gx = np.gradient(arr)
    mag = np.sqrt(gx**2 + gy**2)
    return float((mag > 24).mean())
