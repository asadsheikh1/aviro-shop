"""Postgres access (psycopg 3 async pool) with the ``dependency_timeout target=db`` fault."""

from typing import Any, cast

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aviro.common.config import Settings
from aviro.common.faults import FaultEngine


class Database:
    def __init__(self, settings: Settings, faults: FaultEngine) -> None:
        self.faults = faults
        self.pool = AsyncConnectionPool(
            settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            timeout=settings.db_pool_timeout_s,
            kwargs={
                "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
                "row_factory": dict_row,
                "application_name": f"aviro-{settings.service}",
            },
            open=False,
        )

    async def open(self) -> None:
        await self.pool.open(wait=False)

    async def close(self) -> None:
        await self.pool.close()

    async def maybe_hang(self, conn: Any) -> None:
        hang = self.faults.dependency_hang_s("db")
        if hang:
            # A real slow query: Postgres cancels it at statement_timeout.
            await conn.execute("SELECT pg_sleep(%s)", (hang,))

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            await self.maybe_hang(conn)
            cur = await conn.execute(sql, params)
            # rows are dicts (row_factory=dict_row); psycopg's typing can't see that
            return cast(list[dict[str, Any]], await cur.fetchall())

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def ping(self) -> bool:
        try:
            async with self.pool.connection(timeout=1.0) as conn:
                await conn.execute("SELECT 1")
        except Exception:
            return False
        return True
