import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from database import get_connection, init_db
from repo_resolver import ensure_repo

load_dotenv(Path(__file__).with_name(".env"))


def fetch_github_api(endpoint, extra_headers=None, paginate=False):
    """Wraps GitHub CLI (gh api) via subprocess. Builds command, captures output, parses JSON.
    Branches on paginate flag: concatenates multi-page responses by replacing ][ with comma.
    Uses UTF-8 encoding with error=replace to handle encoding mismatches. Prints error to stdout
    on subprocess or JSON decode failure. Returns None if stdout is empty or exception occurs.
    Guarantees: idempotent (no side effects on GitHub). No connection pooling or retry logic.
    Args:
        endpoint: GitHub API path (e.g., 'repos/owner/repo/traffic/views').
        extra_headers: Optional list of -H values to pass to gh api.
        paginate: If True, appends --paginate and merges concatenated JSON arrays.
    Returns:
        dict or list: Parsed JSON, or None if response was empty or error occurred.
    """
    cmd = ["gh", "api", endpoint]
    if paginate:
        cmd.append("--paginate")
    if extra_headers:
        for header in extra_headers:
            cmd.extend(["-H", header])
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True
        )
        if not res.stdout.strip():
            return None

        # gh --paginate outputs concatenated JSON arrays/objects
        if paginate and res.stdout.count("[") > 1:
            raw = res.stdout.replace("][", ",")
            return json.loads(raw)

        return json.loads(res.stdout)
    except Exception as e:
        print(f"[!] API Error on {endpoint}: {e}")
        return None


def is_snapshot_stale(cursor, repo_id: int, source_type: str, incoming_items: list) -> bool:
    """Detects frozen GitHub API responses by comparing incoming traffic snapshot to latest SQLite row.
    Branches: (1) empty incoming_items → returns False (new data); (2) no historical row → returns False;
    (3) exact match on all items, counts, uniques → returns True (stale cache). Uses sqlite3 cursor,
    queries traffic_sources table, performs sorted tuple comparison. Idempotent read-only.
    Args:
        cursor: sqlite3 cursor with row_factory=sqlite3.Row.
        repo_id: Repository ID in traffic_sources.
        source_type: 'referrer' or 'path'.
        incoming_items: List of dicts from GitHub API (keys: referrer|path, count, uniques).
    Returns:
        bool: True if incoming snapshot is 100% identical to latest logged row (stale).
    """
    if not incoming_items:
        return False

    # Get the date of the most recent snapshot for this repository and type
    cursor.execute("""
        SELECT MAX(logged_date) FROM traffic_sources
        WHERE repo_id = ? AND source_type = ?
    """, (repo_id, source_type))
    latest_date_row = cursor.fetchone()
    latest_date = latest_date_row[0] if latest_date_row and latest_date_row[0] else None

    if not latest_date:
        return False  # No historical snapshot exists, data is new

    # Fetch items from that latest snapshot
    cursor.execute("""
        SELECT source_or_path, count, uniques
        FROM traffic_sources
        WHERE repo_id = ? AND source_type = ? AND logged_date = ?
        ORDER BY source_or_path
    """, (repo_id, source_type, latest_date))
    db_rows = cursor.fetchall()

    if not db_rows:
        return False

    # Standardize DB records into sorted tuples
    db_snapshot = sorted([(row[0], row[1], row[2]) for row in db_rows])

    # Standardize incoming items based on source_type
    key_name = 'referrer' if source_type == 'referrer' else 'path'
    incoming_snapshot = sorted([
        (item.get(key_name), item.get('count'), item.get('uniques'))
        for item in incoming_items
    ])

    # If all items, counts, and uniques match perfectly, GitHub served a stale cached response
    return db_snapshot == incoming_snapshot


def sync_repository(owner_repo):
    """Fetches all GitHub repository traffic metrics via GitHub CLI and upserts into SQLite.
    Branches: (1) daily_traffic (views/clones time-series); (2) traffic_sources (referrers snapshot);
    (3) traffic_sources (paths snapshot). For each branch, calls fetch_github_api, checks staleness
    via is_snapshot_stale, and conditionally inserts or skips via INSERT ON CONFLICT. Only writes
    daily_traffic rows where BOTH views and clones data exist (intersection) to prevent false zeros.
    Uses repo_resolver.ensure_repo to reuse canonical case-variant row. Prints staleness warnings
    to stdout. Mutates database via upsert operations. Idempotent: ON CONFLICT DO UPDATE ensures
    re-runs overwrite prior snapshots without duplication errors.
    Args:
        owner_repo: Repository name in 'owner/repo' format (canonical after ensure_repo).
    Returns:
        None (side effect: mutates SQLite database).
    """
    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    # Reuse any existing case-variant row instead of creating a duplicate.
    repo_row = ensure_repo(cursor, owner_repo)
    repo_id = repo_row["id"]
    owner_repo = repo_row["owner_repo"]

    today = datetime.now().strftime("%Y-%m-%d")

    # 1. Traffic Views & Clones
    views_data = fetch_github_api(f"repos/{owner_repo}/traffic/views") or {}
    clones_data = fetch_github_api(f"repos/{owner_repo}/traffic/clones") or {}

    views_by_date = {v["timestamp"][:10]
        : v for v in views_data.get("views", [])}
    clones_by_date = {c["timestamp"][:10]
        : c for c in clones_data.get("clones", [])}

    # Only write rows for dates where GitHub returned data for BOTH views and clones.
    # Using intersection prevents false zeros when GitHub's API partially freezes.
    for dt in set(views_by_date.keys()).intersection(set(clones_by_date.keys())):
        v = views_by_date[dt]
        c = clones_by_date[dt]
        cursor.execute("""
            INSERT INTO daily_traffic (repo_id, date, views_count, views_uniques, clones_count, clones_uniques)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id, date) DO UPDATE SET
                views_count = EXCLUDED.views_count, views_uniques = EXCLUDED.views_uniques,
                clones_count = EXCLUDED.clones_count, clones_uniques = EXCLUDED.clones_uniques
        """, (repo_id, dt, v["count"], v["uniques"], c["count"], c["uniques"]))

    # 2. Referrers & Popular Paths (With Staleness Check & Smart Ingestion)
    referrers = fetch_github_api(
        f"repos/{owner_repo}/traffic/popular/referrers") or []
    if is_snapshot_stale(cursor, repo_id, 'referrer', referrers):
        print(f"[!] Warning: Incoming referrers snapshot for {owner_repo} is identical to the previous record. Skipping insertion.")
    else:
        for r in referrers:
            cursor.execute("""
                INSERT INTO traffic_sources (repo_id, logged_date, source_type, source_or_path, count, uniques)
                VALUES (?, ?, 'referrer', ?, ?, ?)
                ON CONFLICT(repo_id, logged_date, source_type, source_or_path) DO UPDATE SET
                    count = EXCLUDED.count, uniques = EXCLUDED.uniques
            """, (repo_id, today, r.get("referrer"), r.get("count"), r.get("uniques")))

    paths = fetch_github_api(f"repos/{owner_repo}/traffic/popular/paths") or []
    if is_snapshot_stale(cursor, repo_id, 'path', paths):
        print(f"[!] Warning: Incoming paths snapshot for {owner_repo} is identical to the previous record. Skipping insertion.")
    else:
        for p in paths:
            cursor.execute("""
                INSERT INTO traffic_sources (repo_id, logged_date, source_type, source_or_path, title, count, uniques)
                VALUES (?, ?, 'path', ?, ?, ?, ?)
                ON CONFLICT(repo_id, logged_date, source_type, source_or_path) DO UPDATE SET
                    title = EXCLUDED.title, count = EXCLUDED.count, uniques = EXCLUDED.uniques
            """, (repo_id, today, p.get("path"), p.get("title"), p.get("count"), p.get("uniques")))

    # 3. All Historical Stargazers (Paginated)
    stars = fetch_github_api(
        f"repos/{owner_repo}/stargazers",
        extra_headers=["Accept: application/vnd.github.v3.star+json"],
        paginate=True
    ) or []
    for s in stars:
        if isinstance(s, dict) and "user" in s:
            cursor.execute("""
                INSERT OR IGNORE INTO stars_history (repo_id, user_handle, starred_at)
                VALUES (?, ?, ?)
            """, (repo_id, s["user"]["login"], s["starred_at"]))

    # 4. Release Downloads
    releases = fetch_github_api(f"repos/{owner_repo}/releases") or []
    for rel in releases:
        tag = rel.get("tag_name")
        for asset in rel.get("assets", []):
            cursor.execute("""
                INSERT INTO releases_history (repo_id, logged_date, tag_name, asset_name, download_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, logged_date, tag_name, asset_name) DO UPDATE SET
                    download_count = EXCLUDED.download_count
            """, (repo_id, today, tag, asset.get("name"), asset.get("download_count")))

    # 5. Code Frequency & Activity Stats
    code_freq = fetch_github_api(
        f"repos/{owner_repo}/stats/code_frequency") or []
    for week in code_freq:
        # week is [timestamp, additions, deletions]
        if isinstance(week, list) and len(week) == 3:
            ts = datetime.fromtimestamp(week[0]).strftime("%Y-%m-%d")
            cursor.execute("""
                INSERT INTO community_stats (repo_id, week_timestamp, additions, deletions, commits)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(repo_id, week_timestamp) DO UPDATE SET
                    additions = EXCLUDED.additions, deletions = EXCLUDED.deletions
            """, (repo_id, ts, week[1], week[2]))

    conn.commit()
    conn.close()
    print(f"[+] Successfully synced ALL metrics for {owner_repo}")


if __name__ == "__main__":
    repo = os.getenv("GITHUB_REPO", "").strip()
    if not repo:
        raise SystemExit(
            "GITHUB_REPO is not set. Copy .env.example to .env and set it in the "
            'format "owner/repo", or pass a repo via the web UI or MCP.'
        )
    sync_repository(repo)