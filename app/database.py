from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()


def _to_asyncpg_url(url: str) -> str:
    """Neon gives out a psycopg-style DSN (postgresql://...?sslmode=require&channel_binding=require).
    asyncpg doesn't understand sslmode/channel_binding query params, and SSL is instead
    passed via connect_args, so strip them and swap the driver."""
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.pop("sslmode", None)
    query.pop("channel_binding", None)
    new_query = urlencode(query)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


ASYNC_DATABASE_URL = _to_asyncpg_url(settings.database_url)

engine = create_async_engine(
    ASYNC_DATABASE_URL,
    connect_args={"ssl": True},
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
