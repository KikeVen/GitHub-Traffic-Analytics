import asyncio
import json
import os
import sqlite3

from mcp.server.mcpserver import MCPServer

from ingester import sync_repository

# Resolve absolute path based on this file's location
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "app.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# Initialize the MCP server wrapper using the v2 API class
mcp = MCPServer("GitHub Analytics")


@mcp.tool()
def get_repo_metrics(repo: str, start_date: str, end_date: str) -> str:
    """Fetches daily traffic metrics (views, clones) for a repository within a date range (YYYY-MM-DD)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        query = '''
            SELECT d.date, d.views_count, d.views_uniques, d.clones_count, d.clones_uniques
            FROM daily_traffic d
            JOIN repositories r ON d.repo_id = r.id
            WHERE LOWER(r.owner_repo) LIKE LOWER(?) AND d.date BETWEEN ? AND ?
            ORDER BY d.date ASC
        '''
        cursor.execute(query, (f"%{repo}%", start_date, end_date))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return json.dumps(rows, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_events(repo: str) -> str:
    """Returns logged external articles, Hacker News spikes, or product launches for a repository (includes event IDs for editing/deletion)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='external_events'")
        table_name = "external_events" if cursor.fetchone() else "events"

        query = f'''
            SELECT e.id, e.event_date, e.title, e.description, e.url, e.category
            FROM {table_name} e
            JOIN repositories r ON e.repo_id = r.id
            WHERE LOWER(r.owner_repo) LIKE LOWER(?)
            ORDER BY e.event_date DESC
        '''
        cursor.execute(query, (f"%{repo}%",))
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return json.dumps(rows, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def log_external_event(repo: str, date: str, title: str, category: str, description: str, url: str) -> str:
    """Logs an external event. Category, description, and url are strictly required to maintain data integrity."""
    if not all([category, description, url]):
        return "Error: 'category', 'description', and 'url' are required fields and cannot be empty."

    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='external_events'")
        table_name = "external_events" if cursor.fetchone() else "events"

        cursor.execute(
            "SELECT id FROM repositories WHERE LOWER(owner_repo) LIKE LOWER(?)", (f"%{repo}%",))
        repo_row = cursor.fetchone()
        if not repo_row:
            conn.close()
            return f"Error: Repository '{repo}' not found in the database."

        repo_id = repo_row['id']
        cursor.execute(f'''
            INSERT INTO {table_name} (repo_id, event_date, title, description, url, category)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (repo_id, date, title, description, url, category))
        conn.commit()
        conn.close()
        return f"Success: Event '{title}' [{category}] logged for {repo} on {date}."
    except Exception as e:
        return f"Error logging event: {e!s}"


@mcp.tool()
def update_external_event(event_id: int, date: str = None, title: str = None, category: str = None, description: str = None, url: str = None) -> str:
    """Updates an existing external event by its unique ID. Pass only the fields you wish to change."""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='external_events'")
        table_name = "external_events" if cursor.fetchone() else "events"

        # Check if event exists
        cursor.execute(
            f"SELECT id FROM {table_name} WHERE id = ?", (event_id,))
        if not cursor.fetchone():
            conn.close()
            return f"Error: Event ID {event_id} not found."

        fields = []
        values = []
        if date is not None:
            fields.append("event_date = ?")
            values.append(date)
        if title is not None:
            fields.append("title = ?")
            values.append(title)
        if category is not None:
            fields.append("category = ?")
            values.append(category)
        if description is not None:
            fields.append("description = ?")
            values.append(description)
        if url is not None:
            fields.append("url = ?")
            values.append(url)

        if not fields:
            conn.close()
            return "Error: No update parameters provided."

        values.append(event_id)
        query = f"UPDATE {table_name} SET {', '.join(fields)} WHERE id = ?"
        cursor.execute(query, values)
        conn.commit()
        conn.close()
        return f"Success: Event ID {event_id} updated successfully."
    except Exception as e:
        return f"Error updating event: {e!s}"


@mcp.tool()
def delete_external_event(event_id: int) -> str:
    """Deletes an external event from the database permanently using its unique ID."""
    try:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='external_events'")
        table_name = "external_events" if cursor.fetchone() else "events"

        cursor.execute(f"DELETE FROM {table_name} WHERE id = ?", (event_id,))
        if cursor.rowcount == 0:
            conn.close()
            return f"Error: Event ID {event_id} not found."

        conn.commit()
        conn.close()
        return f"Success: Event ID {event_id} deleted successfully."
    except Exception as e:
        return f"Error deleting event: {e!s}"


@mcp.tool()
async def trigger_ingester(repo: str) -> str:
    """Triggers the GitHub API ingestion sync on demand for a given repository (e.g., owner/repo) asynchronously."""
    try:
        # Offloads synchronous CLI/DB operations to a worker thread so event loop remains unblocked
        await asyncio.to_thread(sync_repository, repo)
        return f"Success: Ingested latest metrics and traffic data for repository '{repo}'."
    except Exception as e:
        return f"Error triggering ingester: {e!s}"


@mcp.tool()
def query_sql(query: str) -> str:
    """Raw SELECT access for open-ended data analysis on the SQLite database."""
    if not query.strip().upper().startswith("SELECT"):
        return "Error: Only SELECT queries are permitted for raw SQL analysis."

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        rows = [dict(row) for row in cursor.fetchall()]
        return json.dumps(rows, indent=2)
    except Exception as e:
        return f"SQL Error: {e!s}"
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()