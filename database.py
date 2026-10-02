import os
import sqlite3

# Resolve absolute path based on database.py's location on your filesystem
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "app.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()

        # Repositories table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS repositories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_repo TEXT UNIQUE NOT NULL,
            is_active INTEGER DEFAULT 1,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Daily time-series traffic (Views & Clones)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_traffic (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            views_count INTEGER DEFAULT 0,
            views_uniques INTEGER DEFAULT 0,
            clones_count INTEGER DEFAULT 0,
            clones_uniques INTEGER DEFAULT 0,
            UNIQUE(repo_id, date),
            FOREIGN KEY (repo_id) REFERENCES repositories(id)
        )
        """)

        # Referrers and Popular Paths
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS traffic_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            logged_date TEXT NOT NULL,
            source_type TEXT NOT NULL, -- 'referrer' or 'path'
            source_or_path TEXT NOT NULL,
            title TEXT,
            count INTEGER DEFAULT 0,
            uniques INTEGER DEFAULT 0,
            UNIQUE(repo_id, logged_date, source_type, source_or_path),
            FOREIGN KEY (repo_id) REFERENCES repositories(id)
        )
        """)

        # UNIQUE INDEX ensures duplicate snapshot rows are blocked across existing DB instances
        cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_traffic_sources_unique 
        ON traffic_sources(repo_id, logged_date, source_type, source_or_path)
        """)

        # Stargazers History
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS stars_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            user_handle TEXT NOT NULL,
            starred_at TEXT NOT NULL,
            UNIQUE(repo_id, user_handle, starred_at),
            FOREIGN KEY (repo_id) REFERENCES repositories(id)
        )
        """)

        # Release Downloads
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS releases_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            logged_date TEXT NOT NULL,
            tag_name TEXT NOT NULL,
            asset_name TEXT NOT NULL,
            download_count INTEGER DEFAULT 0,
            UNIQUE(repo_id, logged_date, tag_name, asset_name),
            FOREIGN KEY (repo_id) REFERENCES repositories(id)
        )
        """)

        # External Events (Articles, Reddit, Launches, etc.)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS external_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            event_date TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            url TEXT,
            category TEXT DEFAULT 'general',
            FOREIGN KEY (repo_id) REFERENCES repositories(id)
        )
        """)

        # Community & Code Frequency Stats
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS community_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL,
            week_timestamp TEXT NOT NULL,
            additions INTEGER DEFAULT 0,
            deletions INTEGER DEFAULT 0,
            commits INTEGER DEFAULT 0,
            UNIQUE(repo_id, week_timestamp),
            FOREIGN KEY (repo_id) REFERENCES repositories(id)
        )
        """)

        conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized successfully at {DB_PATH}")