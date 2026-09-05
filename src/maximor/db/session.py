from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from maximor.config import get_database_settings


@lru_cache
def get_engine():
    """Create the shared engine only when database access is first requested."""

    settings = get_database_settings()
    return create_async_engine(
        settings.database_url.get_secret_value(),
        echo=settings.database_echo,
        pool_pre_ping=True,
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_engine(), class_=AsyncSession, expire_on_commit=False
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a transaction-scoped async session."""

    async with get_session_factory()() as session:
        async with session.begin():
            yield session
