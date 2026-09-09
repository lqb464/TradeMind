"""Optional OCR pipeline for scanned financial reports, distilled from dOCRead."""
from __future__ import annotations
from dataclasses import dataclass
from io import BytesIO

@dataclass
class ParsedReport:
    pages: list[str]
    parser: str
    used_ocr: bool

def _edit_distance(a: list[str], b: list[str]) -> int:
    prev=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        cur=[i]
        for j,y in enumerate(b,1): cur.append(min(cur[-1]+1,prev[j]+1,prev[j-1]+(x!=y)))
        prev=cur
    return prev[-1]
def character_error_rate(pred: str, target: str) -> float: return _edit_distance(list(pred),list(target))/max(1,len(target))
def word_error_rate(pred: str, target: str) -> float: return _edit_distance(pred.split(),target.split())/max(1,len(target.split()))

def _ocr_image(data: bytes) -> str:
    try:
        import numpy as np
        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR
        result,_=RapidOCR()(np.array(Image.open(BytesIO(data)).convert("RGB")))
        return "\n".join(item[1] for item in (result or []))
    except Exception: return ""

def parse_pdf(data: bytes, min_chars_per_page: int = 40) -> ParsedReport:
    from pypdf import PdfReader
    pages=[page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages]
    if pages and sum(len(x.strip()) for x in pages)/len(pages)>=min_chars_per_page: return ParsedReport(pages,"pypdf",False)
    try:
        import fitz
        doc=fitz.open(stream=data,filetype="pdf"); ocr=[]
        for page in doc:
            pix=page.get_pixmap(matrix=fitz.Matrix(1.7,1.7),alpha=False); ocr.append(_ocr_image(pix.tobytes("png")))
        if any(x.strip() for x in ocr): return ParsedReport(ocr,"rapidocr",True)
    except Exception: pass
    return ParsedReport(pages,"pypdf-no-ocr-engine",False)
