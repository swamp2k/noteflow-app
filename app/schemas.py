from datetime import datetime
from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    username: str
    password: str


class UserLogin(BaseModel):
    username: str
    password: str
    totp_code: str | None = None


class UserResponse(BaseModel):
    id: int
    email: str
    username: str
    totp_enabled: bool
    google_linked: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AttachmentResponse(BaseModel):
    id: int
    filename: str
    mime_type: str
    size_bytes: int
    extracted_text: str | None = None

    model_config = {"from_attributes": True}


class NoteResponse(BaseModel):
    id: int
    content: str
    source: str
    original_date: datetime | None = None
    ai_tags: list[str] | None = None
    is_starred: bool
    is_public: bool = False
    public_slug: str | None = None
    created_at: datetime
    updated_at: datetime
    attachments: list[AttachmentResponse] = []

    model_config = {"from_attributes": True}


class NoteCreate(BaseModel):
    content: str


class NotePatch(BaseModel):
    content: str | None = None
    is_starred: bool | None = None
    is_public: bool | None = None


class ApiKeyUpdate(BaseModel):
    api_key: str


class NoteListResponse(BaseModel):
    notes: list[NoteResponse]
    total: int
    page: int
    page_size: int


class TOTPSetupResponse(BaseModel):
    secret: str
    qr_data_url: str


class GraphNoteItem(BaseModel):
    id: int
    content: str
    tags: list[str]
    date: datetime
    x: float
    y: float
    has_embedding: bool


class GraphTagItem(BaseModel):
    tag: str
    count: int


class GraphCooccurrence(BaseModel):
    a: str
    b: str
    count: int


class GraphCluster(BaseModel):
    label: str
    cx: float
    cy: float
    note_ids: list[int]


class GraphResponse(BaseModel):
    notes: list[GraphNoteItem]
    tags: list[GraphTagItem]
    cooccurrence: list[GraphCooccurrence]
    clusters: list[GraphCluster]
