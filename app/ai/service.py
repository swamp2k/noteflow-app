import asyncio
import base64
from typing import Any
from app.config import settings


def _client(api_key: str | None = None):
    key = api_key or settings.anthropic_api_key
    if not key:
        return None
    import anthropic
    return anthropic.AsyncAnthropic(api_key=key)


async def generate_tags(content: str, api_key: str | None = None) -> list[str]:
    client = _client(api_key)
    if not client:
        return []
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=128,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Extract 1-5 short topic tags (single words or short phrases, lowercase, no #) "
                        "from the following note. Return only a JSON array of strings, nothing else.\n\n"
                        f"{content[:2000]}"
                    ),
                }
            ],
        )
        import json
        text = response.content[0].text.strip()
        tags = json.loads(text)
        return [str(t).lower() for t in tags if isinstance(t, str)][:5]
    except Exception:
        return []


async def ocr_image(image_bytes: bytes, mime_type: str, api_key: str | None = None) -> str:
    client = _client(api_key)
    if not client:
        return ""
    try:
        b64 = base64.standard_b64encode(image_bytes).decode()
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime_type, "data": b64},
                        },
                        {"type": "text", "text": "Extract all text visible in this image. Return only the extracted text, nothing else."},
                    ],
                }
            ],
        )
        return response.content[0].text.strip()
    except Exception:
        return ""


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages[:20]:  # limit to first 20 pages
            parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    except Exception:
        return ""


async def generate_embedding(text: str, api_key: str | None = None) -> list[float] | None:
    """Generate a text embedding using Voyage AI via Anthropic API."""
    key = api_key or settings.anthropic_api_key
    if not key:
        return None
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=key)
        response = await client.embeddings.create(
            model="voyage-3",
            input=text[:8000],
        )
        return response.embeddings[0].embedding
    except Exception:
        return None


def extract_docx_text(docx_bytes: bytes) -> str:
    try:
        import io
        from docx import Document
        doc = Document(io.BytesIO(docx_bytes))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:
        return ""


def compute_umap_positions(notes_with_embeddings: list[Any]) -> dict[int, tuple[float, float]]:
    """Compute 2D UMAP positions for notes with embeddings."""
    if len(notes_with_embeddings) < 4:
        import random
        return {n.id: (random.random(), random.random()) for n in notes_with_embeddings}
    try:
        import numpy as np
        import umap as umap_module

        ids = [n.id for n in notes_with_embeddings]
        vectors = np.array([n.embedding for n in notes_with_embeddings], dtype=np.float32)

        n_neighbors = min(15, len(ids) - 1)
        reducer = umap_module.UMAP(n_components=2, random_state=42, min_dist=0.3, n_neighbors=n_neighbors)
        coords = reducer.fit_transform(vectors)

        # Normalise to 0..1
        coords -= coords.min(axis=0)
        max_vals = coords.max(axis=0)
        max_vals[max_vals == 0] = 1  # avoid divide-by-zero
        coords /= max_vals

        return {ids[i]: (float(coords[i][0]), float(coords[i][1])) for i in range(len(ids))}
    except Exception:
        import random
        return {n.id: (random.random(), random.random()) for n in notes_with_embeddings}
