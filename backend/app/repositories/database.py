"""The DuckDB connection: one serialized writer and a bounded pool of read cursors,
both off the event loop, with timeouts and a clean shutdown."""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb

from app.config import get_settings
from app.repositories.schema import create_schema

log = logging.getLogger("cpi.db")

READ_POOL_SIZE = 4
# On shutdown, a running write (an import batch) gets this long to finish
# before it is interrupted. Interrupting is safe: rows and file offset are
# committed together, so an interrupted batch is simply imported again.
SHUTDOWN_WRITE_WAIT_S = 5.0

# Dedicated, bounded threads for DB work instead of asyncio's shared default
# executor: reads can never occupy more threads than there are cursors, and
# the single writer always has its own thread.
_read_executor = ThreadPoolExecutor(READ_POOL_SIZE, thread_name_prefix="duckdb-read")
_write_executor = ThreadPoolExecutor(1, thread_name_prefix="duckdb-write")


class DBBusyError(Exception):
    """The database could not serve the request in time (mapped to HTTP 503)."""


def _duckdb_config() -> dict:
    # DuckDB defaults to 80% of the RAM it can see — inside Docker that is the
    # whole VM, not the container limit — and to one thread per CPU. Together
    # with the Python heap this pushed the process past the VM memory and got
    # it OOM-killed. Keep the engine inside an explicit budget instead.
    settings = get_settings()
    config: dict[str, str | int] = {
        "memory_limit": settings.duckdb_memory_limit,
        "threads": settings.duckdb_threads,
        "checkpoint_threshold": settings.duckdb_checkpoint_threshold,
    }
    if settings.duckdb_temp_dir:
        config["temp_directory"] = settings.duckdb_temp_dir
    return config


def _consume_result(fut: asyncio.Future) -> None:
    """Mark the outcome of a shielded DB call as retrieved. When the caller timed
    out or was cancelled, nobody awaits the future any more, and asyncio would log
    the (expected) InterruptException as "Future exception was never retrieved"."""
    if not fut.cancelled():
        fut.exception()


class Database:
    """Singleton async wrapper around a synchronous DuckDB connection.

    Writes (INSERT/UPDATE/DELETE) go through the single main connection,
    serialized by `_write_lock` — DuckDB only allows one writer at a time.

    Reads (SELECT) go through a small pool of `cursor()` objects derived from
    that same connection. DuckDB cursors share the underlying database handle
    and can run concurrently with an in-progress write from another cursor
    (MVCC snapshot reads) — this is what keeps the UI (tenants, stats, log
    browsing) responsive while a large fetch/import job is writing in the
    background, instead of queuing behind it.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        """Wrap an open connection whose schema is up to date; see open()."""
        self._conn = conn
        self._closed = False
        self._write_lock = asyncio.Lock()
        self._cursors = [conn.cursor() for _ in range(READ_POOL_SIZE)]
        self._read_pool: asyncio.Queue = asyncio.Queue()
        for cur in self._cursors:
            self._read_pool.put_nowait(cur)

    @classmethod
    def open(cls, path: Path) -> "Database":
        """Connect with the configured memory budget and bring the schema up to date."""
        conn = duckdb.connect(str(path), config=_duckdb_config())
        try:
            memory_limit, threads = conn.execute(
                "SELECT current_setting('memory_limit'), current_setting('threads')"
            ).fetchall()[0]
            log.info(f"duckdb {duckdb.__version__}: memory_limit={memory_limit}, threads={threads}")
            create_schema(conn)
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    @staticmethod
    def execute(cur, sql: str, params=None):
        if params:
            return cur.execute(sql, params)
        return cur.execute(sql)

    @classmethod
    def fetch_all(cls, cur, sql: str, params=None) -> list[dict]:
        c = cls.execute(cur, sql, params)
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row, strict=True)) for row in c.fetchall()]

    @classmethod
    def fetch_one(cls, cur, sql: str, params=None) -> dict | None:
        c = cls.execute(cur, sql, params)
        if c.description is None:
            return None
        cols = [d[0] for d in c.description]
        # fetchall(), not fetchone(): a partially consumed result keeps the
        # cursor's query open, and in DuckDB < 1.5 that blocks every automatic
        # CHECKPOINT — the writer (and with it the fetch job) then hangs until
        # the cursor happens to be reused. All callers select at most one row.
        rows = c.fetchall()
        return dict(zip(cols, rows[0], strict=True)) if rows else None

    @classmethod
    def fetch_val(cls, cur, sql: str, params=None):
        c = cls.execute(cur, sql, params)
        rows = c.fetchall()  # see fetch_one
        return rows[0][0] if rows else None

    async def run(self, fn, *args, **kwargs):
        """Write path: run a synchronous DB function against the main connection,
        serialized by `_write_lock`, off the event loop (in a worker thread) so a
        long-running write never blocks other requests from being scheduled.

        The lock is released only once the in-flight call has actually finished
        (via a done-callback), even if the awaiting request is cancelled early —
        otherwise a new writer could start against the same connection while the
        abandoned one is still running, corrupting/blocking future calls."""
        try:
            # How long a write waits for the single writer before giving up (HTTP 503).
            await asyncio.wait_for(self._write_lock.acquire(), get_settings().write_lock_timeout_s)
        except TimeoutError:
            log.warning(f"gave up waiting {get_settings().write_lock_timeout_s:.0f}s for the write lock")
            raise DBBusyError("database busy: another write is still running") from None
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_write_executor, lambda: fn(self._conn, *args, **kwargs))

        def release(f: asyncio.Future) -> None:
            _consume_result(f)
            self._write_lock.release()

        fut.add_done_callback(release)
        return await asyncio.shield(fut)

    async def read(self, fn, *args, **kwargs):
        """Read path: borrow a cursor from the pool and run a synchronous SELECT
        against it, off the event loop. Runs concurrently with `run()` writes and
        with other `read()` calls (bounded by READ_POOL_SIZE).

        Backpressure and timeouts, so slow or abandoned queries can't lock up
        the API for minutes:
        - waiting for a free cursor is bounded (POOL_ACQUIRE_TIMEOUT_S → DBBusyError, HTTP 503);
        - a query running longer than QUERY_TIMEOUT_S, or whose caller was
          cancelled, is stopped with `cursor.interrupt()`.

        `asyncio.shield` + a done-callback still make sure the cursor only
        returns to the pool once its query has actually finished, so a new
        reader never gets a cursor that is still executing."""
        try:
            cur = await asyncio.wait_for(self._read_pool.get(), get_settings().pool_acquire_timeout_s)
        except TimeoutError:
            raise DBBusyError("database busy: no free read connection") from None
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_read_executor, lambda: fn(cur, *args, **kwargs))

        def give_back(f: asyncio.Future) -> None:
            _consume_result(f)
            self._read_pool.put_nowait(cur)

        fut.add_done_callback(give_back)
        t0 = time.perf_counter()
        try:
            return await asyncio.wait_for(asyncio.shield(fut), get_settings().query_timeout_s)
        except TimeoutError:
            cur.interrupt()
            log.warning(f"query interrupted after {time.perf_counter() - t0:.1f}s (timeout)")
            raise DBBusyError(f"query took longer than {get_settings().query_timeout_s:g}s and was cancelled") from None
        except asyncio.CancelledError:
            cur.interrupt()
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self, write_wait_s: float = SHUTDOWN_WRITE_WAIT_S) -> None:
        """Shut down cleanly: let the running write finish (interrupt it after
        write_wait_s), stop running reads, CHECKPOINT so the WAL is folded into
        the database file, and close. The wrapper is unusable afterwards."""
        if self._closed:
            return
        self._closed = True
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(self._write_lock.acquire(), write_wait_s)
        except TimeoutError:
            log.warning("shutdown: interrupting the running write after %.0fs", write_wait_s)
            self._conn.interrupt()
            await self._write_lock.acquire()
        if self._read_pool.qsize() < READ_POOL_SIZE:
            for cur in self._cursors:
                cur.interrupt()
            for _ in range(READ_POOL_SIZE):
                await self._read_pool.get()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_write_executor, self._checkpoint_and_close)
        log.info("database checkpointed and closed in %.1fs", time.perf_counter() - t0)

    def _checkpoint_and_close(self) -> None:
        try:
            self._conn.execute("CHECKPOINT")
        except duckdb.Error as e:
            log.warning("shutdown: checkpoint failed (%s); the WAL is replayed on the next start", e)
        for cur in self._cursors:
            cur.close()
        self._conn.close()
