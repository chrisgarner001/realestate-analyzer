"""
Database models and session factory for the RE Investor Analyzer.
Uses PostgreSQL in production (DATABASE_URL env var) or SQLite locally.
"""
from sqlalchemy import (create_engine, Column, Integer, String, Text, Boolean,
                         DateTime, ForeignKey)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.pool import StaticPool
from datetime import datetime
from pathlib import Path
import tempfile
import time
import os
from urllib.parse import urlsplit, urlunsplit

DATABASE_URL = os.getenv("DATABASE_URL")  # set in Cloud Run env vars

if DATABASE_URL:
    # ── PostgreSQL (production) ──────────────────────────────────────
    # Supabase requires sslmode=require; append if not already in URL.
    # This covers both direct URLs and the pooler host used by Supabase.
    db_url = DATABASE_URL
    if "supabase" in db_url.lower() and "sslmode" not in db_url:
        separator = "&" if "?" in db_url else "?"
        db_url += f"{separator}sslmode=require"
    # Vercel functions need Supabase's transaction pooler, not session mode.
    parsed_url = urlsplit(db_url)
    if "pooler.supabase.com" in parsed_url.hostname and parsed_url.port == 5432:
        db_url = urlunsplit((parsed_url.scheme, parsed_url.netloc.replace(":5432", ":6543"), parsed_url.path, parsed_url.query, parsed_url.fragment))
    # Reuse warm connections across invocations on the same serverless instance
    # instead of reconnecting every request; pool_recycle avoids handing out
    # connections the DB/pooler may have silently dropped while idle.
    engine = create_engine(
        db_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=2,
        pool_timeout=10,
        pool_recycle=280,
        connect_args={
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        },
    )
else:
    # ── SQLite (local development) ───────────────────────────────────
    # Vercel's deployed application directory is read-only; SQLite there is
    # only a temporary fallback until DATABASE_URL is configured.
    DB_ROOT = Path(tempfile.gettempdir()) if os.getenv("VERCEL") else Path(__file__).parent / "data"
    DB_PATH = DB_ROOT / "realestate.db"
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Tenant(Base):
    __tablename__ = "tenants"
    id              = Column(Integer, primary_key=True, index=True)
    slug            = Column(String(80),  unique=True, nullable=False, index=True)
    company_name    = Column(String(200), nullable=False)
    logo_url        = Column(Text)
    primary_color   = Column(String(20),  default="#2d8a4e")
    tagline         = Column(String(200), default="AI-powered property intelligence")
    welcome_message = Column(String(500))
    contact_name    = Column(String(200))
    contact_phone   = Column(String(50))
    contact_email   = Column(String(200))
    contact_nmls    = Column(String(50))
    daily_limit     = Column(Integer,     default=5)
    token_balance   = Column(Integer,     default=0)
    is_active       = Column(Boolean,     default=True)
    created_at      = Column(DateTime,    default=datetime.utcnow)
    invite_template = Column(Text)   # admin-saved JSON: master invite email copy + logo

    users    = relationship("User",     back_populates="tenant", cascade="all, delete-orphan")
    analyses = relationship("Analysis", back_populates="tenant")


class User(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True, index=True)
    tenant_id     = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    email         = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    full_name     = Column(String(200))
    role          = Column(String(20),  default="realtor")   # realtor | admin | superadmin
    is_active     = Column(Boolean,     default=True)
    created_at    = Column(DateTime,    default=datetime.utcnow)
    last_login    = Column(DateTime)
    token_balance = Column(Integer,     default=0)
    usage_reset_at = Column(DateTime)

    tenant   = relationship("Tenant",   back_populates="users")
    analyses = relationship("Analysis", back_populates="user")


class Analysis(Base):
    __tablename__ = "analyses"
    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"))
    tenant_id     = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    address       = Column(String(500))
    asking_price  = Column(String(50))
    analysis_type = Column(String(50))
    created_at    = Column(DateTime, default=datetime.utcnow)
    ip_address    = Column(String(50))

    user   = relationship("User",   back_populates="analyses")
    tenant = relationship("Tenant", back_populates="analyses")


class BuyerLead(Base):
    __tablename__ = "buyer_leads"
    id                 = Column(Integer, primary_key=True, index=True)
    analysis_id        = Column(Integer, nullable=False, index=True)
    tenant_id          = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    agent_id           = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    buyer_name         = Column(String(200), nullable=False)
    buyer_email        = Column(String(200), nullable=False, index=True)
    report_text        = Column(String, nullable=True)
    buyer_email_sent   = Column(Boolean, default=False)
    sponsor_email_sent = Column(Boolean, default=False)
    created_at         = Column(DateTime, default=datetime.utcnow)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables and seed the superadmin account from env vars."""
    # A transient connect failure here (e.g. a DB pooler restart) would
    # otherwise crash the whole app's cold start until the next deploy.
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(bind=engine)
            break
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(2 * attempt)
    # Keep existing pilot databases compatible as new billing fields land.
    from sqlalchemy import inspect, text
    if not DATABASE_URL:
        columns = {column["name"] for column in inspect(engine).get_columns("tenants")}
        if "tagline" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE tenants ADD COLUMN tagline VARCHAR(200) DEFAULT 'AI-powered property intelligence'"))
        columns = {column["name"] for column in inspect(engine).get_columns("tenants")}
        if "token_balance" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE tenants ADD COLUMN token_balance INTEGER DEFAULT 0"))
        columns = {column["name"] for column in inspect(engine).get_columns("users")}
        if "usage_reset_at" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE users ADD COLUMN usage_reset_at DATETIME"))
        columns = {column["name"] for column in inspect(engine).get_columns("users")}
        if "token_balance" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE users ADD COLUMN token_balance INTEGER DEFAULT 0"))

    # Runs on both SQLite and Postgres since this column was added after tables
    # already existed in production, so create_all alone won't add it there.
    tenant_columns = {column["name"] for column in inspect(engine).get_columns("tenants")}
    if "invite_template" not in tenant_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE tenants ADD COLUMN invite_template TEXT"))

    email    = os.getenv("SUPER_ADMIN_EMAIL",    "admin@yourapp.com")
    password = os.getenv("SUPER_ADMIN_PASSWORD", "changeme123")

    db = SessionLocal()
    try:
        existing = db.query(User).filter_by(email=email).first()
        if not existing:
            from auth import hash_password
            admin = User(
                email=email,
                password_hash=hash_password(password),
                full_name="Super Admin",
                role="superadmin",
                tenant_id=None,
                is_active=True,
            )
            db.add(admin)
            db.commit()
            print(f"  OK Superadmin created: {email}")
        else:
            print(f"  OK Superadmin exists: {email}")
    finally:
        db.close()
