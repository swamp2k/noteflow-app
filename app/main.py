from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.config import settings
from app.auth.router import router as auth_router
from app.notes.router import router as notes_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    await init_db()
    yield


app = FastAPI(title="NoteFlow", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(notes_router)

static_dir = Path(__file__).parent / "static"

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(str(static_dir / "index.html"))

app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
