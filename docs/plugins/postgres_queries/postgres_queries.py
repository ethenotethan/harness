"""Postgres read plugin for artifact queries — persisted, parameterized SQL.

Install
-------
    cp postgres_queries.py ~/.hermes/plugins/actions/
    mkdir -p ~/.hermes/plugins/actions/postgres_queries
    cp -r statements ~/.hermes/plugins/actions/postgres_queries/
    pip install "psycopg[binary]"          # in the gateway's environment
    export HERMES_PG_DSN="postgresql://reader:...@db.internal:5432/shop"
    # then: ask the agent to reload actions, or call the actions.reload RPC

Each ``statements/<name>.sql`` becomes the query handler ``postgres.<name>``.
The file leads with its parameter schema in a comment header, then the SQL,
using psycopg's named placeholders — never string formatting:

    -- params: {"state": {"type": "enum", "values": ["open", "closed"], "default": "open"},
    --          "limit": {"type": "int", "min": 1, "max": 500, "default": 100}}
    SELECT id, customer, total, created_at
    FROM orders
    WHERE state = %(state)s
    ORDER BY created_at DESC
    LIMIT %(limit)s;

An artifact then declares ``{"id": "open-orders", "query": "postgres.orders.open",
"bind": {"state": "open"}}`` and its page may vary ``limit``. The page never
sees the SQL; the artifact never carries it; the gateway validates every
parameter against the header before this file runs anything.

Read-only by construction: every connection sets
``default_transaction_read_only = on`` and a statement timeout, so a
statement that tries to write fails in the database, not in review.

Change notification (optional): set ``HERMES_PG_LISTEN_CHANNEL`` and have a
trigger ``NOTIFY`` that channel on writes. The listener thread calls
``mark_query_changed("postgres")`` and every subscribed dashboard re-runs at
once instead of at its next poll — the gateway still emits only if the rows
actually differ.
"""

import json
import os
import re
import threading
from decimal import Decimal
from pathlib import Path

STATEMENTS_DIR = Path(__file__).with_suffix("") / "statements"
STATEMENT_TIMEOUT_MS = int(os.environ.get("HERMES_PG_STATEMENT_TIMEOUT_MS", "5000"))
MAX_ROWS = 1000

_HEADER_RE = re.compile(r"^\s*--\s?(.*)$")


def _dsn() -> str:
    dsn = os.environ.get("HERMES_PG_DSN", "").strip()
    if not dsn:
        config = Path(__file__).with_suffix("") / "config.json"
        if config.exists():
            dsn = json.loads(config.read_text()).get("dsn", "").strip()
    if not dsn:
        raise QueryError("postgres plugin has no DSN — set HERMES_PG_DSN on the gateway host")
    return dsn


def _parse_statement(path: Path) -> tuple[dict, str]:
    """Split the ``-- params:`` header from the SQL body."""
    header_lines: list[str] = []
    body_lines: list[str] = []
    in_header = True
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _HEADER_RE.match(line) if in_header else None
        if match and not body_lines:
            header_lines.append(match.group(1))
            continue
        in_header = False
        body_lines.append(line)
    header = "\n".join(header_lines)
    schema: dict = {}
    marker = header.find("params:")
    if marker != -1:
        try:
            schema = json.loads(header[marker + len("params:"):])
        except ValueError as exc:
            raise ValueError(f"{path.name}: params header is not valid JSON ({exc})") from None
        if not isinstance(schema, dict):
            raise ValueError(f"{path.name}: params header must be a JSON object")
    sql = "\n".join(body_lines).strip()
    if not sql:
        raise ValueError(f"{path.name}: no SQL after the header")
    return schema, sql


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _make_handler(name: str, sql: str):
    def handler(artifact_id, query_id, params, cursor):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise QueryError("psycopg is not installed in the gateway environment") from None
        # Autocommit + read-only: no transaction to leave open, no way to write.
        with psycopg.connect(
            _dsn(), autocommit=True, row_factory=dict_row,
            options=f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}",
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        rows = [{k: _json_safe(v) for k, v in row.items()} for row in rows[:MAX_ROWS]]
        return {"data": {"rows": rows, "truncated": truncated}}
    handler.__name__ = f"postgres_{name.replace('.', '_')}"
    return handler


def _register_all() -> list[str]:
    registered: list[str] = []
    if not STATEMENTS_DIR.exists():
        logger.warning("postgres plugin: no statements directory at %s", STATEMENTS_DIR)
        return registered
    for path in sorted(STATEMENTS_DIR.glob("*.sql")):
        schema, sql = _parse_statement(path)
        name = f"postgres.{path.stem}"
        register_query_handler(name, _make_handler(name, sql), params=schema)
        registered.append(name)
    logger.info("postgres plugin: registered %s", registered)
    return registered


def _listen_forever(channel: str) -> None:
    try:
        import psycopg
    except ImportError:
        logger.warning("postgres plugin: psycopg missing, change notifications disabled")
        return
    while True:
        try:
            with psycopg.connect(_dsn(), autocommit=True) as conn:
                conn.execute(f'LISTEN "{channel}"')
                for _notify in conn.notifies():
                    mark_query_changed("postgres")
        except Exception as exc:  # noqa: BLE001 — reconnect loop
            logger.warning("postgres plugin: LISTEN dropped (%s); retrying", exc)
            threading.Event().wait(5.0)


_register_all()

_channel = os.environ.get("HERMES_PG_LISTEN_CHANNEL", "").strip()
if _channel:
    threading.Thread(
        target=_listen_forever, args=(_channel,),
        name="postgres-query-listen", daemon=True,
    ).start()
