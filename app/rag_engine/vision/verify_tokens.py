"""关键 token 检测与二次验证（P4，docx 六）。

当问题或首次读取结果包含 数字/尺寸/型号/Block 字母/编号 等关键 token 时，
不直接相信第一次读取，触发“只看该区域、逐字符重读、候选清单、uncertain 兜底”
的验证轮；多次读数不一致时按多数/置信度降级。
"""

from __future__ import annotations

import re

_UNIT = (
    r"(?:mm|cm|m|km|kV|V|A|Hz|%|MPa|kPa|psi|nm|µm|um|m²|mm²|°C|kg|kN|kN/m|NGN|USD|m/s)"
)
_CRIT_RE = re.compile(
    r"("
    rf"\d[\d,./]*\s*{_UNIT}"
    r"|[Bb]lock\s*[A-Z](?:\b|$)"
    r"|(?:Model|Type|No\.?|Ref\.?|Spec)\s*[A-Z0-9][A-Za-z0-9\-/]*"
    r"|\b[A-Z]{1,3}[-]?\d{1,4}(?:[-/][A-Z0-9]+)?\b"
    r"|\b[A-Z]{2,4}[-\s]?\d{2,4}[A-Z]?\b"
    r")",
    re.I,
)

_NUM_RE = re.compile(r"[-+]?\d[\d,./]*\.?\d*")


def extract_critical(text: str) -> list[str]:
    """提取文本中的关键 token（去重、保序）。"""
    out: list[str] = []
    for m in _CRIT_RE.finditer(text or ""):
        tok = m.group(1).strip()
        if tok and tok not in out:
            out.append(tok)
    return out


def _norm(s: str) -> str:
    s = s.strip().lower().replace(",", "").replace(" ", "")
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def reads_agree(a: str, b: str) -> bool:
    """两次读数是否一致（归一化后比较，容忍大小写/逗号/空格/单位冗余）。"""
    if not a or not b:
        return False
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def ocr_numbers(img) -> list[str]:
    """tesseract OCR 提取带单位数值（第三票交叉验证，P4）。"""
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError:
        return []
    try:
        g = img.convert("L")
        g = ImageOps.autocontrast(g)
        g = g.resize((g.width * 3, g.height * 3), Image.LANCZOS)
        txt = pytesseract.image_to_string(g, config="--psm 6")
        return re.findall(r"\d+(?:\.\d+)?\s*(?:mm|nm|µm|um|kV|kN|m)?%?", txt, re.I)
    except Exception:
        return []


def replace_critical(answer: str, from_tok: str, to_tok: str) -> str:
    """把答案中的关键 token 替换为候选值（仅精确字面替换）。"""
    if not from_tok or not to_tok or from_tok == to_tok:
        return answer
    return re.sub(re.escape(from_tok), to_tok, answer, flags=re.I)
