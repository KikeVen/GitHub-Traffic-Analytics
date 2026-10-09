"""MCP server exposing GitHub traffic analytics tools to LLM agents.

Design contract: **resolve or refuse, never guess.**

Every read resolves the repository to exactly one row and echoes the resolved
``repo_id``/``owner_repo`` back. Every write validates its inputs, returns the
exact persisted row, and raises ``ToolError`` (which sets ``is_error=True``) on
any anticipated failure instead of returning a string that looks like success.
"""

import asyncio
import json
import os
import sqlite3

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import repo_resolver as rr
from ingester import sync_repository

# Absolute directory path of the module root. Initialized via os.path.dirname(abspath(__file__)).
# Scopes database paths to this directory. Used by DB_PATH and all table operations.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Path to SQLite database file (app.db) within BASE_DIR. Initialized via os.path.join.
# Used by get_connection() to open or create the database file.
DB_PATH = os.path.join(BASE_DIR, "app.db")


def get_connection():
    """Opens a new SQLite connection to DB_PATH with row factory set to sqlite3.Row.
    Enables dict-like access to rows via column names. No connection pooling or caching.
    Returns:
        sqlite3.Connection: Active connection with row_factory=sqlite3.Row.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _json(**payload):
    """Serialize a Python dict to indented JSON with ensure_ascii=False.
    Used by all tool return values and error responses to construct machine-readable JSON.
    Returns:
        str: Indented JSON representation of payload with 2-space indentation.
    """
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _fail(message: str, **extra):
    """Build a ToolError carrying a machine-readable error payload."""
    return ToolError(_json(ok=False, error=message, **extra))


def _events_table(cursor) -> str:
    """Detects which events table exists in the database schema.
    Returns 'external_events' if it exists, otherwise 'events' (legacy fallback).
    Queries sqlite_master to check table presence without requiring schema migration.
    Returns:
        str: 'external_events' or 'events' depending on what exists in schema.
    """
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='external_events'")
    return "external_events" if cursor.fetchone() else "events"


# Initialize the MCP server wrapper using the v2 API class
mcp = MCPServer("GitHub Analytics")


@mcp.tool()
def list_repositories() -> str:
    """Lists every tracked repository with its numeric ID, canonical name, and a
    duplicate_name flag. Call this before any repo-based tool so you pass a valid
    repo_id instead of guessing a name."""
    conn = get_connection()
    try:
        repos = rr.list_repositories(conn.cursor())
        duplicates = [r for r in repos if r["duplicate_name"]]
        return _json(ok=True, count=len(repos), repositories=repos,
                     duplicate_warning=(
                         "Some names differ only by case; always act by repo_id."
                         if duplicates else None))
    finally:
        conn.close()


@mcp.tool()
def get_repo_metrics(repo: str = None, repo_id: int = None,
                     start_date: str = None, end_date: str = None) -> str:
    """Fetches daily traffic metrics (views, clones) for a repository within an
    optional date range (YYYY-MM-DD). Pass repo_id when known; otherwise pass the
    exact repository name. Ambiguous names are rejected."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            target = rr.resolve_repo(cursor, repo=repo, repo_id=repo_id)
            if start_date is not None:
                start_date = rr.validate_date(start_date, "start_date")
            if end_date is not None:
                end_date = rr.validate_date(end_date, "end_date")
        except (rr.RepoResolutionError, rr.InputValidationError) as exc:
            raise _fail(str(exc)) from exc

        query = '''
            SELECT d.date, d.views_count, d.views_uniques, d.clones_count, d.clones_uniques
            FROM daily_traffic d
            WHERE d.repo_id = ?
        '''
        params = [target["id"]]
        if start_date:
            query += " AND d.date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND d.date <= ?"
            params.append(end_date)
        query += " ORDER BY d.date ASC"

        rows = [dict(r) for r in cursor.execute(query, tuple(params)).fetchall()]
        return _json(ok=True, repo_id=target["id"], repo=target["owner_repo"],
                     start_date=start_date, end_date=end_date,
                     count=len(rows), metrics=rows)
    finally:
        conn.close()


@mcp.tool()
def list_events(repo: str = None, repo_id: int = None) -> str:
    """Returns logged external articles, Hacker News spikes, or product launches
    for a repository (includes event IDs for editing/deletion). Pass repo_id when
    known; ambiguous names are rejected."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            target = rr.resolve_repo(cursor, repo=repo, repo_id=repo_id)
        except rr.RepoResolutionError as exc:
            raise _fail(str(exc)) from exc

        table = _events_table(cursor)
        rows = [dict(r) for r in cursor.execute(f'''
            SELECT e.id, e.event_date, e.title, e.description, e.url, e.category
            FROM {table} e
            WHERE e.repo_id = ?
            ORDER BY e.event_date DESC
        ''', (target["id"],)).fetchall()]
        return _json(ok=True, repo_id=target["id"], repo=target["owner_repo"],
                     count=len(rows), events=rows)
    finally:
        conn.close()


@mcp.tool()
def log_external_event(date: str, title: str, category: str, description: str,
                       url: str, repo: str = None, repo_id: int = None) -> str:
    """Logs an external event. All fields are required and validated. Duplicate
    events (same repo, date, and title) are rejected. Returns the persisted row."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            target = rr.resolve_repo(cursor, repo=repo, repo_id=repo_id)
            date = rr.validate_date(date)
            title = rr.validate_text(title, "title", max_len=rr.MAX_TITLE_LEN)
            category = rr.validate_category(category)
            description = rr.validate_text(
                description, "description", max_len=rr.MAX_DESCRIPTION_LEN)
            url = rr.validate_url(url)
        except (rr.RepoResolutionError, rr.InputValidationError) as exc:
            raise _fail(str(exc)) from exc

        table = _events_table(cursor)

        existing = cursor.execute(
            f"SELECT id FROM {table} WHERE repo_id = ? AND event_date = ? AND title = ?",
            (target["id"], date, title),
        ).fetchone()
        if existing:
            raise _fail(
                f"Duplicate event: an event with this title already exists on {date}.",
                repo_id=target["id"], repo=target["owner_repo"],
                existing_event_id=existing["id"])

        new_id = cursor.execute(f'''
            INSERT INTO {table} (repo_id, event_date, title, description, url, category)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (target["id"], date, title, description, url, category)).lastrowid
        conn.commit()

        row = dict(cursor.execute(
            f"SELECT id, repo_id, event_date, title, description, url, category "
            f"FROM {table} WHERE id = ?", (new_id,)).fetchone())
        return _json(ok=True, message="Event logged.",
                     repo_id=target["id"], repo=target["owner_repo"], event=row)
    finally:
        conn.close()


@mcp.tool()
def update_external_event(event_id: int, date: str = None, title: str = None,
                          category: str = None, description: str = None,
                          url: str = None) -> str:
    """Updates an existing external event by its unique ID. Pass only the fields
    to change. Values are validated; blank values are rejected. Returns the
    updated row."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        table = _events_table(cursor)

        current = cursor.execute(
            f"SELECT id FROM {table} WHERE id = ?", (event_id,)).fetchone()
        if not current:
            raise _fail(f"Event ID {event_id} not found.")

        fields, values = [], []
        try:
            if date is not None:
                fields.append("event_date = ?"); values.append(rr.validate_date(date))
            if title is not None:
                fields.append("title = ?")
                values.append(rr.validate_text(title, "title", max_len=rr.MAX_TITLE_LEN))
            if category is not None:
                fields.append("category = ?"); values.append(rr.validate_category(category))
            if description is not None:
                fields.append("description = ?")
                values.append(rr.validate_text(
                    description, "description", max_len=rr.MAX_DESCRIPTION_LEN))
            if url is not None:
                fields.append("url = ?"); values.append(rr.validate_url(url))
        except rr.InputValidationError as exc:
            raise _fail(str(exc)) from exc

        if not fields:
            raise _fail("No update parameters provided; nothing to change.")

        values.append(event_id)
        cursor.execute(
            f"UPDATE {table} SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()

        row = dict(cursor.execute(
            f"SELECT id, repo_id, event_date, title, description, url, category "
            f"FROM {table} WHERE id = ?", (event_id,)).fetchone())
        return _json(ok=True, message="Event updated.", event=row)
    finally:
        conn.close()


@mcp.tool()
def delete_external_event(event_id: int, confirm: bool = False) -> str:
    """Deletes an external event permanently. Requires confirm=true and returns the
    deleted row so the deletion is auditable."""
    if not confirm:
        raise _fail("Refusing to delete: pass confirm=true to proceed.",
                    event_id=event_id)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        table = _events_table(cursor)
        row = cursor.execute(
            f"SELECT id, repo_id, event_date, title, description, url, category "
            f"FROM {table} WHERE id = ?", (event_id,)).fetchone()
        if not row:
            raise _fail(f"Event ID {event_id} not found.")

        cursor.execute(f"DELETE FROM {table} WHERE id = ?", (event_id,))
        conn.commit()
        return _json(ok=True, message="Event deleted.", deleted=dict(row))
    finally:
        conn.close()


@mcp.tool()
async def trigger_ingester(repo: str = None, repo_id: int = None) -> str:
    """Triggers the GitHub API ingestion sync for a repository. Reuses an existing
    repository row (never creates a case-only duplicate)."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            target = rr.resolve_repo(cursor, repo=repo, repo_id=repo_id)
        except rr.RepoResolutionError as exc:
            raise _fail(str(exc)) from exc
        owner_repo = target["owner_repo"]
    finally:
        conn.close()

    try:
        # Offloads synchronous CLI/DB operations to a worker thread.
        await asyncio.to_thread(sync_repository, owner_repo)
        return _json(ok=True, message="Ingestion complete.",
                     repo_id=target["id"], repo=owner_repo)
    except Exception as exc:
        raise _fail(f"Ingestion failed for '{owner_repo}': {exc!s}") from exc


@mcp.tool()
def query_sql(query: str) -> str:
    """Raw read-only SELECT/WITH access for open-ended data analysis. Single
    statement only; writes are rejected; results are row-capped."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        try:
            safe_query = rr.validate_read_query(query)
        except rr.InputValidationError as exc:
            raise _fail(str(exc)) from exc

        rows = [dict(r) for r in cursor.execute(safe_query).fetchall()]
        return _json(ok=True, count=len(rows), row_cap=rr.MAX_QUERY_ROWS, rows=rows)
    except sqlite3.Error as exc:
        raise _fail(f"SQL error: {exc!s}") from exc
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
