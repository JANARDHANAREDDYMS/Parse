import asyncio
import json
from dataclasses import asdict, dataclass

import typer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from maximor.db.session import get_engine


@dataclass(frozen=True)
class DatabaseHealth:
    connected: bool
    vector_extension_enabled: bool
    vector_extension_version: str | None


async def check_database_health(
    database_engine: AsyncEngine | None = None,
) -> DatabaseHealth:
    """Check connectivity and confirm that the vector extension is enabled."""

    engine = database_engine or get_engine()
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        vector_version = await connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )

    return DatabaseHealth(
        connected=True,
        vector_extension_enabled=vector_version is not None,
        vector_extension_version=vector_version,
    )


def main() -> None:
    """Run the database health check as a standalone command."""

    try:
        result = asyncio.run(check_database_health())
    except (OSError, SQLAlchemyError) as exc:
        typer.echo(
            json.dumps(
                {
                    "connected": False,
                    "vector_extension_enabled": False,
                    "error": exc.__class__.__name__,
                }
            )
        )
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(asdict(result)))
    if not result.vector_extension_enabled:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    main()
