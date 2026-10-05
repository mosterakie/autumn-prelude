"""普通 PDF / DOCX 提取与确定性分块；没有 OCR，也不执行文档内指令。"""

import io
import zipfile
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

from pypdf import PdfReader

from autumn_backend.errors import InvalidInputError
from autumn_backend.io_boundary import require_outside_uow

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str
    locator: dict[str, Any]


def extract(data: bytes, media_type: str) -> tuple[TextPart, ...]:
    require_outside_uow()
    parts = []
    try:
        if media_type == PDF:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted or len(reader.pages) > 500:
                raise InvalidInputError("不支持加密或超过 500 页的 PDF")
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    parts.append(TextPart(text.strip(), {"kind": "pdf", "page": number}))
        elif media_type == DOCX:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                xml = archive.read("word/document.xml")
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise InvalidInputError("文档不支持 XML 实体")
            root = ElementTree.fromstring(xml)
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            for number, paragraph in enumerate(root.iter(f"{namespace}p"), 1):
                text = "".join(item.text or "" for item in paragraph.iter(f"{namespace}t"))
                if text.strip():
                    parts.append(TextPart(text.strip(), {"kind": "docx", "paragraph": number}))
        else:
            raise InvalidInputError("文档类型不支持")
    except InvalidInputError:
        raise
    except Exception as error:
        raise InvalidInputError("文档解析失败") from error
    if not parts or sum(len(part.text) for part in parts) > 2_000_000:
        raise InvalidInputError("文档没有可提取文字或文字过多；首版不支持扫描件 OCR")
    return tuple(parts)


def chunks(
    parts: tuple[TextPart, ...], *, size: int = 1000, overlap: int = 100
) -> tuple[TextPart, ...]:
    if not 0 <= overlap < size <= 4000:
        raise InvalidInputError("分块配置无效")
    result = []
    for part in parts:
        for start in range(0, len(part.text), size - overlap):
            end = min(start + size, len(part.text))
            result.append(
                TextPart(part.text[start:end], {**part.locator, "start": start, "end": end})
            )
            if end == len(part.text):
                break
    if not result or len(result) > 3000:
        raise InvalidInputError("知识内容为空或分块过多")
    return tuple(result)
