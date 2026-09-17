"""Configuration, persistence and cryptographic primitives for the personal gateway."""
import base64
import hashlib
import hmac
import os
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def uid() -> str:
    return uuid.uuid4().hex


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore', hide_input_in_errors=True)
    environment: str = 'production'
    public_origin: str = 'https://localhost'
    database_url: str = 'postgresql+psycopg://gateway:change-me@postgres/gateway'
    redis_url: str = 'redis://redis:6379/0'
    master_key: SecretStr
    admin_username: str = 'owner'
    bootstrap_password: SecretStr
    trust_gateway_ip: bool = False
    session_hours: int = 12
    log_retention_days: int = 30

    @model_validator(mode='after')
    def validate_secrets(self):
        try:
            key = base64.b64decode(self.master_key.get_secret_value(), validate=True)
        except Exception:
            raise ValueError('MASTER_KEY must be base64-encoded 32 random bytes') from None
        if len(key) != 32:
            raise ValueError('MASTER_KEY must decode to exactly 32 bytes')
        if not 16 <= len(self.bootstrap_password.get_secret_value()) <= 128:
            raise ValueError('BOOTSTRAP_PASSWORD must contain 16 to 128 characters')
        self.public_origin = self.public_origin.rstrip('/')
        origin = urlsplit(self.public_origin)
        if origin.scheme not in ('http', 'https') or not origin.hostname or origin.path or origin.query or origin.fragment or origin.username or origin.password:
            raise ValueError('PUBLIC_ORIGIN must be an origin without credentials, path or query')
        if self.environment not in ('production', 'development', 'test'):
            raise ValueError('Invalid ENVIRONMENT')
        if self.environment == 'production':
            if not self.public_origin.startswith('https://'):
                raise ValueError('Production requires HTTPS PUBLIC_ORIGIN')
            if self.database_url.startswith('sqlite'):
                raise ValueError('Use PostgreSQL in production')
        if not 1 <= self.session_hours <= 24 or not 1 <= self.log_retention_days <= 365:
            raise ValueError('Session or log retention outside safe range')
        return self

    @property
    def secure_cookie(self):
        return self.environment == 'production'

    @property
    def cookie_name(self):
        return '__Host-msg_session' if self.secure_cookie else 'msg_session'


class Crypto:
    def __init__(self, key: str):
        self.key = base64.b64decode(key, validate=True)
        self.aes = AESGCM(self.key)
        self.passwords = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

    def digest(self, value: str, purpose: str) -> str:
        return hmac.new(self.key, (purpose + ':' + value).encode(), hashlib.sha256).hexdigest()

    def encrypt(self, value: str, context: str) -> str:
        nonce = os.urandom(12)
        return base64.b64encode(nonce + self.aes.encrypt(nonce, value.encode(), context.encode())).decode()

    def decrypt(self, value: str, context: str) -> str:
        raw = base64.b64decode(value, validate=True)
        return self.aes.decrypt(raw[:12], raw[12:], context.encode()).decode()

    def verify_password(self, password_hash: str, password: str) -> bool:
        try:
            return self.passwords.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False


class Base(DeclarativeBase):
    pass


class Admin(Base):
    __tablename__ = 'admin'
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(Text)
    session_version: Mapped[int] = mapped_column(default=1)


class Config(Base):
    __tablename__ = 'gateway_config'
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    schema_version: Mapped[int] = mapped_column(default=1)
    master_check: Mapped[str] = mapped_column(Text, default='')
    api_key_hash: Mapped[str] = mapped_column(String(64), default='')
    api_key_hint: Mapped[str] = mapped_column(String(40), default='未生成')
    api_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rpm: Mapped[int] = mapped_column(default=60)
    daily_limit: Mapped[int] = mapped_column(default=1000)
    concurrency: Mapped[int] = mapped_column(default=4)
    max_attempts: Mapped[int] = mapped_column(default=4)
    timeout_seconds: Mapped[int] = mapped_column(default=15)


class Provider(Base):
    __tablename__ = 'providers'
    name: Mapped[str] = mapped_column(String(16), primary_key=True)
    priority: Mapped[int] = mapped_column(default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    cooldown_until: Mapped[float] = mapped_column(Float, default=0)


class Account(Base):
    __tablename__ = 'provider_accounts'
    __table_args__ = (UniqueConstraint('provider', 'name'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(ForeignKey('providers.name'))
    name: Mapped[str] = mapped_column(String(80))
    priority: Mapped[int] = mapped_column(default=0)
    enabled: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(32), default='active')
    attempt_limit: Mapped[int] = mapped_column(default=1000)
    attempts_used: Mapped[int] = mapped_column(default=0)
    rpm: Mapped[int] = mapped_column(default=30)
    cooldown_until: Mapped[float] = mapped_column(Float, default=0)
    reset_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Credential(Base):
    __tablename__ = 'provider_credentials'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey('provider_accounts.id'), index=True)
    name: Mapped[str] = mapped_column(String(80))
    encrypted_key: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    hint: Mapped[str] = mapped_column(String(12))
    enabled: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(32), default='active')
    last_used_at: Mapped[float] = mapped_column(Float, default=0)
    last_error: Mapped[str] = mapped_column(String(64), default='')


class RequestLog(Base):
    __tablename__ = 'request_logs'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    query_digest: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default='api')
    status: Mapped[str] = mapped_column(String(32), default='running')
    provider: Mapped[str] = mapped_column(String(16), default='')
    latency_ms: Mapped[int] = mapped_column(default=0)


class Attempt(Base):
    __tablename__ = 'request_attempts'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    request_id: Mapped[str] = mapped_column(ForeignKey('request_logs.id'), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(32))
    credential_id: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(40), default='in_flight')
    http_status: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Audit(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    event: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(80), default='')


class Database:
    def __init__(self, url: str):
        opts = {'pool_pre_ping': True}
        if url.startswith('sqlite'):
            opts['connect_args'] = {'check_same_thread': False}
            if ':memory:' in url:
                opts['poolclass'] = StaticPool
        if url.startswith('postgresql'):
            opts['connect_args'] = {'connect_timeout': 5, 'options': '-c statement_timeout=3000 -c lock_timeout=1000'}
        self.engine = create_engine(url, **opts)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        with self.sessions.begin() as session:
            yield session

    def initialize(self, settings: Settings, crypto: Crypto):
        Base.metadata.create_all(self.engine)
        with self.session() as db:
            if not db.get(Admin, 1):
                db.add(Admin(id=1, username=settings.admin_username,
                             password_hash=crypto.passwords.hash(settings.bootstrap_password.get_secret_value())))
            cfg = db.get(Config, 1)
            if cfg and cfg.schema_version != 1:
                raise RuntimeError('Unsupported database schema; apply an explicit migration')
            if cfg and crypto.decrypt(cfg.master_check, 'master-check') != 'gateway-v1':
                raise RuntimeError('MASTER_KEY does not match this database')
            if not cfg:
                db.add(Config(id=1, master_check=crypto.encrypt('gateway-v1', 'master-check')))
            if not db.scalars(select(Provider)).first():
                db.add_all([Provider(name=name, priority=i) for i, name in enumerate(('tavily', 'exa', 'brave'))])

    def audit(self, event: str, target: str = ''):
        with self.session() as db:
            db.add(Audit(event=event, target=target))
