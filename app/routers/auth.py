"""Auth routes — delegates to Supabase Auth."""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from app.database import get_supabase

router = APIRouter(prefix="/auth", tags=["auth"])


class SignUpRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str


class SignInRequest(BaseModel):
    email: EmailStr
    password: str


@router.post("/signup")
async def sign_up(body: SignUpRequest):
    sb = get_supabase()
    try:
        resp = sb.auth.sign_up(
            {"email": body.email, "password": body.password,
             "options": {"data": {"full_name": body.full_name}}}
        )
        return {"user": resp.user, "session": resp.session}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/signin")
async def sign_in(body: SignInRequest):
    sb = get_supabase()
    try:
        resp = sb.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
        return {"user": resp.user, "session": resp.session}
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/signout")
async def sign_out(token: str):
    sb = get_supabase()
    sb.auth.sign_out()
    return {"message": "Signed out"}
