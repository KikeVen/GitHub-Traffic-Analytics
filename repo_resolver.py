"""Canonical repository resolution and input validation.

Single source of truth for turning an agent-supplied repository name into exactly
one repository row. The rule is simple: **resolve or refuse, never guess.**

- Fuzzy/substring matching is forbidden. Only exact, case-insensitive names match.
- A name that matches zero rows is an error.
- A name that matches more than one row (legacy duplicate casing) is an error that
  tells the caller to pass an explicit ``repo_id``.
- Inputs (dates, URLs, text) are validated before they ever reach the database.

Both the MCP server and the web API import this module so they cannot diverge.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

from database import get_connection

# --- validation limits -------------------------------------------------------
MAX_TITLE_LEN = 300
MAX_CATEGORY_LEN = 50
MAX_DESCRIPTION_LEN = 4000
MAX_URL_LEN = 2000
MAX_QUERY_ROWS = 500

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RepoResolutionError(Exception):
    """Raised when a repository name/id cannot be resolved to exactly one row."""


class InputValidationError(ValueError):
    """Raised when a supplied field is malformed."""


def _norm(value: str | None) -> str:
    return (value or "").strip()


def _canonical(value: str | None) -> str:
    """Canonical storage/lookup form for owner/repo: trimmed and lower-cased."""
    return _norm(value).lower()


# --- resolution --------------------------------------------------------------

def list_repositories(cursor) -> list[dict]:
    """Return every repository with a flag marking case-insensitive duplicates."""
    rows = cursor.execute(
        "SELECT id, owner_repo, is_active FROM repositories ORDER BY owner_repo"
    ).fetchall()
    rows = [dict(r) for r in rows]

    counts: dict[str, int] = {}
    for r in rows:
        key = r["owner_repo"].lower()
        counts[key] = counts.get(key, 0) + 1

    for r in rows:
        r["duplicate_name"] = counts[r["owner_repo"].lower()] > 1
    return rows


def resolve_repo(cursor, repo: str | None = None, repo_id: int | None = None) -> dict:
    """Resolve to exactly one repository row.

    Prefer ``repo_id``. If only ``repo`` is given it must be an exact,
    case-insensitive, whole-name match. Ambiguity is an error, never a coin flip.
    """
    if repo_id is not None:
        row = cursor.execute(
            "SELECT id, owner_repo FROM repositories WHERE id = ?", (repo_id,)
        ).fetchone()
        if not row:
            known = [r["owner_repo"] for r in cursor.execute(
                "SELECT owner_repo FROM repositories ORDER BY owner_repo")]
            raise RepoResolutionError(
                f"repo_id {repo_id} does not exist. Known repositories: {known}"
            )
        return dict(row)

    name = _canonical(repo)
    if not name:
        raise RepoResolutionError(
            "Provide either 'repo' (owner/name) or 'repo_id'. "
            "Call list_repositories() to see valid values."
        )

    rows = cursor.execute(
        "SELECT id, owner_repo FROM repositories WHERE LOWER(owner_repo) = ?",
        (name,),
    ).fetchall()

    if not rows:
        known = [r["owner_repo"] for r in cursor.execute(
            "SELECT owner_repo FROM repositories ORDER BY owner_repo")]
        raise RepoResolutionError(
            f"Repository '{name}' not found. Known repositories: {known}"
        )

    if len(rows) > 1:
        candidates = [{"repo_id": r["id"], "owner_repo": r["owner_repo"]} for r in rows]
        raise RepoResolutionError(
            f"Repository name '{name}' is ambiguous; it matches {len(rows)} rows "
            f"{candidates}. Pass an explicit repo_id."
        )

    return dict(rows[0])


def ensure_repo(cursor, owner_repo: str) -> dict:
    """Return the existing case-variant row, or insert a new one.

    Used by the ingester so syncing a repo never creates a case-only duplicate.
    """
    raw = _norm(owner_repo)
    if raw.count("/") != 1 or not all(raw.split("/")):
        raise RepoResolutionError(
            f"'{raw}' is not in 'owner/repo' format."
        )
    name = raw.lower()

    rows = cursor.execute(
        "SELECT id, owner_repo FROM repositories WHERE LOWER(owner_repo) = ?",
        (name,),
    ).fetchall()
    if rows:
        # Deterministic: reuse the lowest id (the original row).
        return dict(sorted((dict(r) for r in rows), key=lambda r: r["id"])[0])

    new_id = cursor.execute(
        "INSERT INTO repositories (owner_repo) VALUES (?)", (name,)
    ).lastrowid
    return {"id": new_id, "owner_repo": name}


# --- validation --------------------------------------------------------------

def validate_date(value: str | None, field: str = "date") -> str:
    """Require a real calendar date in strict YYYY-MM-DD form."""
    v = _norm(value)
    if not _DATE_RE.match(v):
        raise InputValidationError(f"'{field}' must be YYYY-MM-DD, got {value!r}.")
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError as exc:
        raise InputValidationError(f"'{field}' is not a valid date: {value!r}.") from exc
    return v


def validate_text(
    value: str | None,
    field: str,
    *,
    required: bool = True,
    max_len: int,
) -> str | None:
    """Trim text, enforce non-empty (when required) and a maximum length."""
    v = _norm(value)
    if not v:
        if required:
            raise InputValidationError(f"'{field}' is required and cannot be empty.")
        return None
    if len(v) > max_len:
        raise InputValidationError(f"'{field}' exceeds {max_len} characters.")
    return v


def validate_url(value: str | None, *, required: bool = True) -> str | None:
    """Require an http(s) URL with a host."""
    v = _norm(value)
    if not v:
        if required:
            raise InputValidationError("'url' is required and cannot be empty.")
        return None
    if len(v) > MAX_URL_LEN:
        raise InputValidationError(f"'url' exceeds {MAX_URL_LEN} characters.")
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InputValidationError(
            f"'url' must be an http(s) URL with a host, got {value!r}."
        )
    return v


def validate_category(value: str | None) -> str:
    """Normalize a category to a trimmed lowercase token."""
    v = _norm(value).lower()
    if not v:
        raise InputValidationError("'category' is required and cannot be empty.")
    if len(v) > MAX_CATEGORY_LEN:
        raise InputValidationError(f"'category' exceeds {MAX_CATEGORY_LEN} characters.")
    return v


# --- SQL safety --------------------------------------------------------------

_BANNED_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|create|replace|"
    r"vacuum|reindex|truncate|grant|revoke)\b",
    re.IGNORECASE,
)


def validate_read_query(query: str) -> str:
    """Restrict raw SQL to a single read-only SELECT/WITH statement with a row cap."""
    if not query or not query.strip():
        raise InputValidationError("'query' cannot be empty.")

    q = query.strip().rstrip(";").strip()
    if ";" in q:
        raise InputValidationError("Only a single SQL statement is allowed.")

    lowered = q.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise InputValidationError("Only SELECT/WITH read queries are permitted.")

    if _BANNED_SQL.search(q):
        raise InputValidationError("Only read-only queries are permitted.")

    if " limit " not in lowered:
        q = f"{q} LIMIT {MAX_QUERY_ROWS}"
    return q


__all__ = [
    "RepoResolutionError",
    "InputValidationError",
    "get_connection",
    "list_repositories",
    "resolve_repo",
    "ensure_repo",
    "validate_date",
    "validate_text",
    "validate_url",
    "validate_category",
    "validate_read_query",
    "MAX_TITLE_LEN",
    "MAX_DESCRIPTION_LEN",
    "MAX_QUERY_ROWS",
]
