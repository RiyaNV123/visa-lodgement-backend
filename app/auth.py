import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.config import settings
from app.models import User
from app.sheet_store import StoreError, find_user_by_id

# Every request pays this lookup, so a blip here is the most visible/common
# way Apps Script's own occasional flakiness (rate limits, transient
# execution failures -- see sheet_store.py's per-call latency notes) reaches
# a user, on an endpoint that otherwise has nothing to do with the request
# they were making. These blips have consistently cleared within a couple of
# seconds when observed directly, so a couple of short retries here absorbs
# most of them before giving up with a 503.
USER_LOOKUP_RETRY_DELAYS_SECONDS = (0.5, 1.5)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(user: User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user.id), "role": user.role.value, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_error
    except JWTError:
        raise credentials_error

    last_error: StoreError | None = None
    for delay in (0, *USER_LOOKUP_RETRY_DELAYS_SECONDS):
        if delay:
            time.sleep(delay)
        try:
            user = find_user_by_id(int(user_id))
            break
        except StoreError as exc:
            last_error = exc
    else:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google Sheets is temporarily unavailable") from last_error
    if user is None:
        raise credentials_error
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role.value != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
