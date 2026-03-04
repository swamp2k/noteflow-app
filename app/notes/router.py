import asyncio
import io
import os
import random
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import Attachment, Note, User
from app.schemas import (
    GraphCluster,
    GraphCooccurrence,
    GraphNoteItem,
    GraphResponse,
    GraphTagItem,
    NoteListResponse,
    NoteResponse,
    NotePatch,
)
import app.ai.service as ai_svc

router = APIRouter(prefix="/api/notes", tags=["notes"])

import re
def _strip_html(text: str) -> str:
    return re.sub(r'<[^>]+>', ' ', text).strip()


def _note_response(note: Note) -> dict:
    return {
        "id": note.id,
        "content": note.content,
        "source": note.source,
        "original_date": note.original_date,
        "ai_tags": note.ai_tags or [],
        "is_starred": note.is_starred,
        "is_public": note.is_public,
        "public_slug": note.public_slug,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
        "attachments": [
            {
                "id": a.id,
                "filename": a.filename,
                "mime_type": a.mime_type,
                "size_bytes": a.size_bytes,
                "extracted_text": a.extracted_text,
            }
            for a in note.attachments
        ],
    }


async def _save_attachment(note_id: int, upload: UploadFile, db: AsyncSession) -> Attachment:
    data = await upload.read()
    ext = Path(upload.filename or "file").suffix
    stored_name = uuid.uuid4().hex + ext

    upload_path = Path(settings.upload_dir)
    upload_path.mkdir(parents=True, exist_ok=True)
    (upload_path / stored_name).write_bytes(data)

    extracted_text = ""
    if upload.content_type and upload.content_type.startswith("image/"):
        extracted_text = await ai_svc.ocr_image(data, upload.content_type)
    elif upload.content_type == "application/pdf":
        extracted_text = ai_svc.extract_pdf_text(data)
    elif upload.content_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        extracted_text = ai_svc.extract_docx_text(data)

    att = Attachment(
        note_id=note_id,
        filename=upload.filename or stored_name,
        stored_name=stored_name,
        mime_type=upload.content_type or "application/octet-stream",
        size_bytes=len(data),
        extracted_text=extracted_text or None,
    )
    db.add(att)
    return att


@router.get("", response_model=NoteListResponse)
async def list_notes(
    q: str | None = None,
    tag: str | None = None,
    source: str | None = None,
    starred: bool | None = None,
    shared: bool | None = None,
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Note).where(Note.user_id == user.id)

    if shared is True:
        stmt = stmt.where(Note.is_public == True)
    elif source:
        stmt = stmt.where(Note.source == source)
    else:
        stmt = stmt.where(Note.source == "local")

    if starred is not None:
        stmt = stmt.where(Note.is_starred == starred)

    if tag:
        stmt = stmt.where(Note.ai_tags.contains(tag))

    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Note.content.ilike(like), Note.search_text.ilike(like))
        )

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    stmt = stmt.order_by(Note.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    notes = result.scalars().all()

    # eagerly load attachments
    from sqlalchemy.orm import selectinload
    stmt2 = select(Note).where(Note.id.in_([n.id for n in notes])).options(selectinload(Note.attachments)).order_by(Note.created_at.desc())
    result2 = await db.execute(stmt2)
    notes = result2.scalars().all()

    return {
        "notes": [_note_response(n) for n in notes],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("", response_model=NoteResponse, status_code=201)
async def create_note(
    content: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = Note(user_id=user.id, content=content, source="local")
    db.add(note)
    await db.flush()  # get note.id

    attachment_texts = []
    for upload in files:
        if upload.filename:
            att = await _save_attachment(note.id, upload, db)
            if att.extracted_text:
                attachment_texts.append(att.extracted_text)

    note.search_text = _strip_html(content) + " " + " ".join(attachment_texts)
    await db.commit()
    await db.refresh(note)

    # Use a simple approach: run tags sync for reliability
    tags = await ai_svc.generate_tags(content, api_key=user.anthropic_api_key)
    if tags:
        note.ai_tags = tags

    embedding = await ai_svc.generate_embedding(content, api_key=user.anthropic_api_key)
    if embedding:
        note.embedding = embedding

    await db.commit()
    await db.refresh(note)

    from sqlalchemy.orm import selectinload
    stmt = select(Note).where(Note.id == note.id).options(selectinload(Note.attachments))
    result = await db.execute(stmt)
    note = result.scalar_one()

    return _note_response(note)


@router.get("/graph", response_model=GraphResponse)
async def get_graph(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    stmt = select(Note).where(Note.user_id == user.id).options(selectinload(Note.attachments))
    result = await db.execute(stmt)
    all_notes = result.scalars().all()

    # Compute UMAP positions for notes with embeddings
    notes_with_emb = [n for n in all_notes if n.embedding]
    positions = ai_svc.compute_umap_positions(notes_with_emb) if notes_with_emb else {}

    # Tag counts and co-occurrence
    tag_counts: dict[str, int] = defaultdict(int)
    cooc: dict[tuple[str, str], int] = defaultdict(int)

    note_items = []
    for note in all_notes:
        tags = note.ai_tags or []
        for t in tags:
            tag_counts[t] += 1
        for i, ta in enumerate(tags):
            for tb in tags[i + 1:]:
                key = tuple(sorted([ta, tb]))
                cooc[key] += 1

        if note.id in positions:
            x, y = positions[note.id]
            has_emb = True
        else:
            x, y = random.random(), random.random()
            has_emb = False

        note_items.append(GraphNoteItem(
            id=note.id,
            content=note.content[:120],
            tags=tags,
            date=note.created_at,
            x=x,
            y=y,
            has_embedding=has_emb,
        ))

    # Simple tag-cluster: group notes by most common tag
    tag_note_map: dict[str, list[int]] = defaultdict(list)
    for note in all_notes:
        tags = note.ai_tags or []
        if tags:
            tag_note_map[tags[0]].append(note.id)

    clusters = []
    for label, note_ids in list(tag_note_map.items())[:8]:
        matching = [ni for ni in note_items if ni.id in note_ids]
        if matching:
            cx = sum(n.x for n in matching) / len(matching)
            cy = sum(n.y for n in matching) / len(matching)
            clusters.append(GraphCluster(label=label, cx=cx, cy=cy, note_ids=note_ids))

    return GraphResponse(
        notes=note_items,
        tags=[GraphTagItem(tag=t, count=c) for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])[:50]],
        cooccurrence=[GraphCooccurrence(a=k[0], b=k[1], count=v) for k, v in cooc.items()],
        clusters=clusters,
    )


@router.get("/public/{slug}")
async def get_public_note(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    stmt = select(Note).where(Note.public_slug == slug, Note.is_public == True).options(selectinload(Note.attachments))
    result = await db.execute(stmt)
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(404, "Note not found")
    return _note_response(note)


@router.get("/{note_id}", response_model=NoteResponse)
async def get_note(
    note_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    stmt = select(Note).where(Note.id == note_id, Note.user_id == user.id).options(selectinload(Note.attachments))
    result = await db.execute(stmt)
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(404, "Note not found")
    return _note_response(note)


@router.patch("/{note_id}", response_model=NoteResponse)
async def patch_note(
    note_id: int,
    body: NotePatch,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    stmt = select(Note).where(Note.id == note_id, Note.user_id == user.id).options(selectinload(Note.attachments))
    result = await db.execute(stmt)
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(404, "Note not found")

    if body.content is not None:
        note.content = body.content
        note.search_text = _strip_html(body.content)
        tags = await ai_svc.generate_tags(body.content, api_key=user.anthropic_api_key)
        if tags:
            note.ai_tags = tags
    if body.is_starred is not None:
        note.is_starred = body.is_starred
    if body.is_public is not None:
        note.is_public = body.is_public
        if body.is_public and note.public_slug is None:
            import secrets
            note.public_slug = secrets.token_urlsafe(12)

    await db.commit()
    await db.refresh(note)
    return _note_response(note)


@router.delete("/{note_id}", status_code=204)
async def delete_note(
    note_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Note).where(Note.id == note_id, Note.user_id == user.id)
        .options(selectinload(Note.attachments))
    )
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(404, "Note not found")

    # Delete attachment files
    upload_path = Path(settings.upload_dir)
    for att in note.attachments:
        try:
            (upload_path / att.stored_name).unlink(missing_ok=True)
        except Exception:
            pass

    await db.delete(note)
    await db.commit()


@router.get("/{note_id}/attachments/{att_id}/file")
async def download_attachment(
    note_id: int,
    att_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Note).where(Note.id == note_id, Note.user_id == user.id))
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(404, "Note not found")

    result = await db.execute(select(Attachment).where(Attachment.id == att_id, Attachment.note_id == note_id))
    att = result.scalar_one_or_none()
    if not att:
        raise HTTPException(404, "Attachment not found")

    file_path = Path(settings.upload_dir) / att.stored_name
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")

    return FileResponse(str(file_path), media_type=att.mime_type, filename=att.filename)


@router.post("/import/upnote", status_code=201)
async def import_upnote(
    file: UploadFile = File(..., alias="file"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await file.read()
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise HTTPException(400, "Expected a .zip file")

    import re

    created = 0
    skipped = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        md_files = [n for n in zf.namelist() if n.endswith(".md")]
        for name in md_files:
            raw = zf.read(name).decode("utf-8", errors="replace")

            # Parse YAML frontmatter
            original_date = None
            content = raw
            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if fm_match:
                fm = fm_match.group(1)
                content = raw[fm_match.end():]
                date_match = re.search(r"date:\s*(.+)", fm)
                if date_match:
                    try:
                        from dateutil.parser import parse as parse_date
                        original_date = parse_date(date_match.group(1).strip())
                    except Exception:
                        pass

            content_stripped = content.strip()
            if not content_stripped:
                skipped += 1
                continue
            note = Note(
                user_id=user.id,
                content=content_stripped,
                source="upnote",
                original_date=original_date,
                search_text=content_stripped,
            )
            db.add(note)
            created += 1

    await db.commit()
    return {"imported": created, "skipped": skipped}
