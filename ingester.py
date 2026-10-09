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
    """Executes gh CLI and parses JSON output safely with UTF-8 encoding."""
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
    """
    Checks if incoming API snapshot data is 100% identical to the latest logged snapshot.
    Returns True if the incoming data is frozen/stale, False otherwise.
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
    """Fetches ALL repository metrics and stores them in SQLite."""
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

    for dt in set(views_by_date.keys()).union(set(clones_by_date.keys())):
        v = views_by_date.get(dt, {"count": 0, "uniques": 0})
        c = clones_by_date.get(dt, {"count": 0, "uniques": 0})
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