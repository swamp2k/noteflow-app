import io
import base64
from datetime import datetime, timedelta, timezone
import bcrypt
import pyotp
import qrcode
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import User, Session
from app.config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


async def create_user(db: AsyncSession, email: str, username: str, password: str) -> User:
    user = User(
        email=email,
        username=username,
        hashed_pw=hash_password(password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_google_id(db: AsyncSession, google_id: str) -> User | None:
    result = await db.execute(select(User).where(User.google_id == google_id))
    return result.scalar_one_or_none()


async def create_session(db: AsyncSession, user_id: int, user_agent: str | None = None) -> Session:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.session_expire_seconds)
    session = Session(user_id=user_id, expires_at=expires_at, user_agent=user_agent)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def delete_session(db: AsyncSession, token: str) -> None:
    result = await db.execute(select(Session).where(Session.id == token))
    session = result.scalar_one_or_none()
    if session:
        await db.delete(session)
        await db.commit()


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def make_totp_qr_data_url(secret: str, username: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(username, issuer_name="NoteFlow")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"
