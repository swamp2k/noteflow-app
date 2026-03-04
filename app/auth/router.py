from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import httpx

from app.database import get_db
from app.models import User
from app.schemas import UserCreate, UserLogin, UserResponse, TOTPSetupResponse, ApiKeyUpdate, UserPreferences
from app.dependencies import get_current_user
from app.config import settings
import app.auth.service as svc

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_response(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "totp_enabled": user.totp_enabled,
        "google_linked": user.google_id is not None,
        "created_at": user.created_at,
    }


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(body: UserCreate, db: AsyncSession = Depends(get_db)):
    if await svc.get_user_by_email(db, body.email):
        raise HTTPException(400, "Email already registered")
    if await svc.get_user_by_username(db, body.username):
        raise HTTPException(400, "Username taken")
    user = await svc.create_user(db, body.email, body.username, body.password)
    return _user_response(user)


@router.post("/login")
async def login(body: UserLogin, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    user = await svc.get_user_by_username(db, body.username)
    if not user or not user.hashed_pw or not svc.verify_password(body.password, user.hashed_pw):
        raise HTTPException(401, "Invalid credentials")

    if user.totp_enabled:
        if not body.totp_code:
            raise HTTPException(200, "totp_required")  # frontend checks for this
        if not svc.verify_totp(user.totp_secret, body.totp_code):
            raise HTTPException(401, "Invalid 2FA code")

    ua = request.headers.get("user-agent")
    session = await svc.create_session(db, user.id, ua)
    response.set_cookie(
        "session",
        session.id,
        httponly=True,
        samesite="lax",
        max_age=settings.session_expire_seconds,
        secure=settings.base_url.startswith("https"),
    )
    return _user_response(user)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    token = request.cookies.get("session")
    if token:
        await svc.delete_session(db, token)
    response.delete_cookie("session")
    return {"ok": True}


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)):
    return _user_response(user)


@router.post("/2fa/setup", response_model=TOTPSetupResponse)
async def totp_setup(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    secret = svc.generate_totp_secret()
    user.totp_secret = secret
    await db.commit()
    return {
        "secret": secret,
        "qr_data_url": svc.make_totp_qr_data_url(secret, user.username),
    }


@router.post("/2fa/enable")
async def totp_enable(
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    code = body.get("code", "")
    if not user.totp_secret or not svc.verify_totp(user.totp_secret, code):
        raise HTTPException(400, "Invalid code")
    user.totp_enabled = True
    await db.commit()
    return {"ok": True}


@router.post("/2fa/disable")
async def totp_disable(body: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    code = body.get("code", "")
    if not user.totp_secret or not svc.verify_totp(user.totp_secret, code):
        raise HTTPException(400, "Invalid code")
    user.totp_enabled = False
    user.totp_secret = None
    await db.commit()
    return {"ok": True}


@router.put("/apikey")
async def save_api_key(
    body: ApiKeyUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user.anthropic_api_key = body.api_key.strip() or None
    await db.commit()
    return {"ok": True}


@router.get("/apikey-status")
async def apikey_status(user: User = Depends(get_current_user)):
    return {"set": bool(user.anthropic_api_key)}


@router.get("/preferences")
async def get_preferences(user: User = Depends(get_current_user)):
    return user.preferences or {}


@router.patch("/preferences")
async def update_preferences(
    body: UserPreferences,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    prefs = dict(user.preferences or {})
    for field, value in body.model_dump(exclude_none=True).items():
        prefs[field] = value
    user.preferences = prefs
    await db.commit()
    return user.preferences


@router.get("/google-enabled")
async def google_enabled():
    return {"enabled": bool(settings.google_client_id and settings.google_client_secret)}


@router.get("/google")
async def google_redirect():
    if not settings.google_client_id:
        raise HTTPException(501, "Google OAuth not configured")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": f"{settings.base_url}/api/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
    }
    from urllib.parse import urlencode
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@router.get("/google/callback")
async def google_callback(
    code: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": f"{settings.base_url}/api/auth/google/callback",
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo_resp.raise_for_status()
        info = userinfo_resp.json()

    google_id = info["id"]
    email = info.get("email", "")

    user = await svc.get_user_by_google_id(db, google_id)
    if not user:
        user = await svc.get_user_by_email(db, email)
        if user:
            user.google_id = google_id
            await db.commit()
        else:
            username = email.split("@")[0]
            # ensure unique username
            base = username
            counter = 1
            while await svc.get_user_by_username(db, username):
                username = f"{base}{counter}"
                counter += 1
            user = User(email=email, username=username, google_id=google_id)
            db.add(user)
            await db.commit()
            await db.refresh(user)

    ua = request.headers.get("user-agent")
    session = await svc.create_session(db, user.id, ua)
    response.set_cookie(
        "session",
        session.id,
        httponly=True,
        samesite="lax",
        max_age=settings.session_expire_seconds,
        secure=settings.base_url.startswith("https"),
    )
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/")
