import base64
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .models import AuthSession, User

COOKIE_NAME = "navigate_session"
SESSION_TTL = timedelta(days=7)
_ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{2,63}$")
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if not _ACCOUNT_PATTERN.fullmatch(email):
        raise ValueError("账户名需为 3 至 64 位字母、数字或 . _ @ -")
    return email


def validate_password(password: str) -> None:
    if len(password) < 12 or len(password) > 128:
        raise ValueError("密码长度须为 12 至 128 个字符")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_text}${digest_text}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, digest_text = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_user(
    db: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    role: str = "member",
    must_change_password: bool = False,
) -> User:
    clean_name = display_name.strip()
    if not clean_name:
        raise ValueError("显示名称不能为空")
    if role not in {"admin", "member"}:
        raise ValueError("账号角色无效")
    user = User(
        email=normalize_email(email),
        display_name=clean_name,
        password_hash=hash_password(password),
        role=role,
        must_change_password=must_change_password,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("该邮箱已注册") from exc
    db.refresh(user)
    return user


def create_session(db: Session, user: User) -> str:
    raw_token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode("ascii")).hexdigest(),
            expires_at=datetime.now(UTC) + SESSION_TTL,
        )
    )
    user.last_login_at = datetime.now(UTC)
    db.commit()
    return raw_token


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def session_from_token(db: Session, raw_token: str | None) -> AuthSession | None:
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session = db.scalar(
        select(AuthSession).where(
            AuthSession.token_hash == token_hash,
            AuthSession.revoked_at.is_(None),
        )
    )
    if session is None or _as_utc(session.expires_at) <= datetime.now(UTC):
        return None
    return session


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth_session = session_from_token(db, request.cookies.get(COOKIE_NAME))
    user = db.get(User, auth_session.user_id) if auth_session else None
    if user is None or not user.is_active:
        raise HTTPException(401, "请先登录")
    return user
