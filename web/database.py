"""
Database models and session factory for PropYield.
Uses PostgreSQL in production (DATABASE_URL env var) or SQLite locally.
"""
from sqlalchemy import (create_engine, Column, Integer, String, Text, Boolean,
                         DateTime, Float, ForeignKey, Index, UniqueConstraint, LargeBinary)
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
    tier            = Column(String(20),  default="partner")  # partner | solo
    setup_intent_id = Column(String(200), unique=True)  # solo signup: consumed Stripe SetupIntent, prevents replay
    asset_analysis_enabled = Column(Boolean, default=False)  # owned-asset exit module entitlement
    lc_servicer     = Column(String(100))   # e.g. "SGMS": LC servicing/compliance handled (downgrades flag)
    owned_defaults  = Column(Text)          # JSON: last-used batch assumptions (remembered per tenant)

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
    buyer_name         = Column(String(200), nullable=True)
    buyer_email        = Column(String(200), nullable=True, index=True)
    report_text        = Column(String, nullable=True)
    buyer_email_sent   = Column(Boolean, default=False)
    sponsor_email_sent = Column(Boolean, default=False)
    created_at         = Column(DateTime, default=datetime.utcnow)


class CredentialRegister(Base):
    """Password-manager references for a tenant; password secrets are never stored."""
    __tablename__ = "credential_register"
    id             = Column(Integer, primary_key=True, index=True)
    tenant_id      = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    portal_name    = Column(String(200), nullable=False)
    portal_url     = Column(String(2000))
    admin_username = Column(String(200))
    vault_name     = Column(String(200), nullable=False)
    vault_item     = Column(String(500), nullable=False)
    mfa_status     = Column(String(50), default="Not recorded")
    access_owner   = Column(String(200))
    review_due     = Column(String(10))
    notes          = Column(Text)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MillageRate(Base):
    """
    Levied millage for one taxing jurisdiction, modelled on Michigan's Property
    Tax Estimator: county + city/township/village + school district resolve to a
    homestead (PRE) and a non-homestead rate.
    """
    __tablename__ = "millage_rates"
    id                  = Column(Integer, primary_key=True, index=True)
    state               = Column(String(2),   nullable=False, index=True)
    county              = Column(String(120), nullable=False, index=True)
    jurisdiction        = Column(String(200), nullable=False, index=True)
    school_district     = Column(String(200), default="", index=True)
    homestead_mills     = Column(Float, default=0.0)
    non_homestead_mills = Column(Float, default=0.0)
    tax_year            = Column(Integer, index=True)
    source              = Column(String(300))
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("state", "county", "jurisdiction", "school_district",
                         "tax_year", name="uq_millage_jurisdiction"),
        Index("ix_millage_lookup", "state", "county", "jurisdiction"),
    )


class TokenPurchase(Base):
    """Audit trail for self-serve Stripe token top-ups (one row per Checkout Session)."""
    __tablename__ = "token_purchases"
    id                = Column(Integer, primary_key=True, index=True)
    tenant_id         = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    stripe_session_id = Column(String(200), unique=True, nullable=False, index=True)
    token_count       = Column(Integer, nullable=False)
    amount_cents      = Column(Integer, nullable=False)
    currency          = Column(String(10), default="usd")
    status            = Column(String(20), default="completed")  # completed | failed
    created_at        = Column(DateTime, default=datetime.utcnow)


class RateLimitBucket(Base):
    """Fixed-window rate-limit counter, shared across serverless instances via
    Postgres instead of in-memory state (this app is a single Vercel function
    with no Redis)."""
    __tablename__ = "rate_limit_buckets"
    id           = Column(Integer, primary_key=True, index=True)
    key          = Column(String(300), unique=True, nullable=False, index=True)
    count        = Column(Integer, nullable=False, default=0)
    window_start = Column(DateTime, nullable=False, default=datetime.utcnow)


class OwnedBatch(Base):
    """One owned-asset exit run: an uploaded inventory (kind='batch') or a
    single property (kind='single', a batch of one)."""
    __tablename__ = "owned_batches"
    id             = Column(Integer, primary_key=True, index=True)
    tenant_id      = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    user_id        = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind           = Column(String(20), default="batch")        # batch | single
    file_name      = Column(String(300))
    assumptions    = Column(Text)                                # JSON of owned_asset.Assumptions overrides
    address_hash   = Column(String(64), index=True)              # duplicate-run warning
    summary_emailed = Column(Boolean, default=False)
    created_at     = Column(DateTime, default=datetime.utcnow)
    finished_at    = Column(DateTime)

    rows = relationship("OwnedBatchRow", back_populates="batch", cascade="all, delete-orphan",
                        order_by="OwnedBatchRow.row_number")


class OwnedBatchRow(Base):
    __tablename__ = "owned_batch_rows"
    id            = Column(Integer, primary_key=True, index=True)
    batch_id      = Column(Integer, ForeignKey("owned_batches.id"), nullable=False, index=True)
    row_number    = Column(Integer, nullable=False)
    include       = Column(Boolean, default=True)
    # queued | running | done | failed_refunded | failed_charged | skipped_pending | excluded
    status        = Column(String(30), default="queued", index=True)
    inputs        = Column(Text)        # JSON: parsed CSV fields
    overrides     = Column(Text)        # JSON: Edit-drawer values (loan, payback, basis, ...)
    flags         = Column(Text)        # JSON list of preview flags
    estimates     = Column(Text)        # JSON: DATA from the estimate call, with sources (reused by free recalc)
    analysis      = Column(Text)        # JSON: owned_asset.analyze() output
    rationale     = Column(Text)
    rationale_assumptions = Column(String(64))   # hash of assumptions the rationale was written under
    writeup       = Column(Text)
    writeup_status = Column(String(20))          # None | running | done
    debited       = Column(Integer, default=0)
    error         = Column(Text)
    started_at    = Column(DateTime)
    finished_at   = Column(DateTime)

    batch = relationship("OwnedBatch", back_populates="rows")


class OwnedStatement(Base):
    """Uploaded loan statement (N3). File bytes are kept only until the
    statement is confirmed or 24 hours pass (N-design D5)."""
    __tablename__ = "owned_statements"
    id             = Column(Integer, primary_key=True, index=True)
    batch_id       = Column(Integer, ForeignKey("owned_batches.id"), nullable=False, index=True)
    row_id         = Column(Integer, ForeignKey("owned_batch_rows.id"), nullable=True, index=True)
    file_name      = Column(String(300))
    media_type     = Column(String(100))
    file_bytes     = Column(LargeBinary)
    status         = Column(String(30), default="reading")  # reading | read | unreadable | password | not_statement | too_large | failed | superseded | confirmed
    extracted      = Column(Text)          # JSON: address, payoff, rate, pi, statement_date, lender, page, snippets
    match_tier     = Column(String(20))    # exact | likely | none | multi
    covers         = Column(Integer, default=1)
    confirmed      = Column(Boolean, default=False)
    confirmed_values = Column(Text)        # JSON of confirmed payoff/rate/pi
    statement_date = Column(DateTime)
    created_at     = Column(DateTime, default=datetime.utcnow)
    confirmed_at   = Column(DateTime)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash   = Column(String(64), unique=True, nullable=False, index=True)
    expires_at   = Column(DateTime, nullable=False)
    used_at      = Column(DateTime)
    created_at   = Column(DateTime, default=datetime.utcnow)


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
    tenant_columns = {column["name"] for column in inspect(engine).get_columns("tenants")}
    if "tier" not in tenant_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE tenants ADD COLUMN tier VARCHAR(20) DEFAULT 'partner'"))
    tenant_columns = {column["name"] for column in inspect(engine).get_columns("tenants")}
    if "setup_intent_id" not in tenant_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE tenants ADD COLUMN setup_intent_id VARCHAR(200)"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_tenants_setup_intent_id ON tenants (setup_intent_id)"))

    tenant_columns = {column["name"] for column in inspect(engine).get_columns("tenants")}
    for col, ddl in (("asset_analysis_enabled", "BOOLEAN DEFAULT FALSE"),
                     ("lc_servicer", "VARCHAR(100)"), ("owned_defaults", "TEXT")):
        if col not in tenant_columns:
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE tenants ADD COLUMN {col} {ddl}"))

    # buyer_name/buyer_email became optional; create_all never loosens an
    # existing NOT NULL, so relax it explicitly. SQLite can't ALTER COLUMN at
    # all — local dev DBs pick up the new nullable=True only on a fresh file.
    if DATABASE_URL:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE buyer_leads ALTER COLUMN buyer_name DROP NOT NULL"))
            connection.execute(text("ALTER TABLE buyer_leads ALTER COLUMN buyer_email DROP NOT NULL"))

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
