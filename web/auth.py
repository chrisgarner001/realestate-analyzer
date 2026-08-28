"""
JWT creation/verification and FastAPI auth dependencies.
"""
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
import os
from database import get_db

SECRET_KEY   = os.getenv("JWT_SECRET", "CHANGE-ME-use-openssl-rand-hex-32")
ALGORITHM    = "HS256"
EXPIRE_HOURS = 24

bearer = HTTPBearer(auto_error=False)


# ── Password helpers ─────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ── JWT helpers ───────────────────────────────────────────────────────────────

def create_token(user_id: int, email: str, role: str,
                 tenant_id: Optional[int], slug: Optional[str]) -> str:
    payload = {
        "sub":       str(user_id),
        "email":     email,
        "role":      role,
        "tenant_id": tenant_id,
        "slug":      slug,
        "exp":       datetime.utcnow() + timedelta(hours=EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ── FastAPI dependencies ──────────────────────────────────────────────────────

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
):
    """Require a valid JWT. Returns the User ORM object."""
    from database import User
    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = db.query(User).filter_by(id=int(payload["sub"]), is_active=True).first()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user

def require_admin(user=Depends(get_current_user)):
    if user.role not in ("admin", "superadmin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user

def require_superadmin(user=Depends(get_current_user)):
    if user.role != "superadmin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Superadmin access required")
    return user
