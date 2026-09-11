from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import create_access_token, get_current_user, hash_password, verify_password
from app.models import User, UserRole
from app.schemas import LoginRequest, MeResponse, SignupRequest, TokenResponse
from app.sheet_store import StoreError, create_user, find_user_by_email

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest):
    try:
        # create_user duplicate-checks and inserts in one Apps Script call.
        user = create_user(payload.email, hash_password(payload.password), payload.full_name, UserRole.student)
    except StoreError as exc:
        # Apps Script reports this as the JS error's String() form ("Error:
        # DUPLICATE_EMAIL"), not the bare code, so an exact match here never
        # fires -- every duplicate-signup attempt was falling through to the
        # generic 502 "Google Sheets is unavailable" instead of a real 409.
        if "DUPLICATE_EMAIL" in str(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered") from exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Google Sheets is unavailable: {exc}") from exc
    return TokenResponse(access_token=create_access_token(user))


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest):
    try:
        user = find_user_by_email(payload.email)
    except StoreError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Google Sheets is unavailable: {exc}") from exc
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    return TokenResponse(access_token=create_access_token(user))


@router.get("/me", response_model=MeResponse)
def me(current_user: User = Depends(get_current_user)):
    return current_user
