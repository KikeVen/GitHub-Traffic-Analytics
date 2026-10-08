import sqlite3

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from database import get_connection
from ingester import sync_repository

app = FastAPI(title="GitHub Analytics Dashboard")
templates = Jinja2Templates(directory="templates")

class RepoCreate(BaseModel):
    owner_repo: str

class EventCreate(BaseModel):
    repo_id: int
    event_date: str
    title: str
    description: str | None = None
    url: str | None = None
    category: str | None = "general"

class SyncRequest(BaseModel):
    repo: str

@app.get("/", response_class=HTMLResponse)
def read_dashboard(request: Request):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, owner_repo FROM repositories WHERE is_active = 1")
    repos = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(request=request, name="index.html", context={"repos": repos})

@app.post("/api/sync")
async def sync_repo_data(payload: SyncRequest, background_tasks: BackgroundTasks):
    """Triggers the ingester in a background task so the browser doesn't time out."""
    if not payload.repo:
        return {"status": "error", "message": "Repository name is required."}

    # Run sync in background thread
    background_tasks.add_task(sync_repository, payload.repo)
    return {"status": "success", "message": f"Sync started in background for '{payload.repo}'."}

@app.post("/api/repos")
def add_repository(payload: RepoCreate):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO repositories (owner_repo, is_active) VALUES (?, 1)", (payload.owner_repo,))
        conn.commit()
        repo_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Repository already exists")
    conn.close()
    return {"status": "success", "id": repo_id, "owner_repo": payload.owner_repo}

@app.get("/api/metrics/{repo_id}")
def get_repo_metrics(
    repo_id: int,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None)
):
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Daily time-series traffic
    query = "SELECT date, views_count, views_uniques, clones_count, clones_uniques FROM daily_traffic WHERE repo_id = ?"
    params = [repo_id]

    if start_date:
        query += " AND date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date <= ?"
        params.append(end_date)

    query += " ORDER BY date ASC"

    cursor.execute(query, tuple(params))
    traffic = [dict(row) for row in cursor.fetchall()]

    # Summary Totals for selected date range
    summary_query = """
        SELECT
            COALESCE(SUM(views_count), 0) as total_views,
            COALESCE(SUM(views_uniques), 0) as unique_views,
            COALESCE(SUM(clones_count), 0) as total_clones,
            COALESCE(SUM(clones_uniques), 0) as unique_clones
        FROM daily_traffic WHERE repo_id = ?
    """
    s_params = [repo_id]
    if start_date:
        summary_query += " AND date >= ?"
        s_params.append(start_date)
    if end_date:
        summary_query += " AND date <= ?"
        s_params.append(end_date)

    cursor.execute(summary_query, tuple(s_params))
    summary = dict(cursor.fetchone())

    # 2. Get Referrers and exact Snapshot Date
    cursor.execute("""
        SELECT MAX(logged_date) FROM traffic_sources
        WHERE repo_id = ? AND source_type = 'referrer'
    """, (repo_id,))
    ref_date_row = cursor.fetchone()
    referrers_date = ref_date_row[0] if ref_date_row and ref_date_row[0] else None

    referrers = []
    if referrers_date:
        cursor.execute("""
            SELECT source_or_path as source, count, uniques
            FROM traffic_sources
            WHERE repo_id = ? AND source_type = 'referrer' AND logged_date = ?
            ORDER BY count DESC
        """, (repo_id, referrers_date))
        referrers = [dict(row) for row in cursor.fetchall()]

    # 3. Get Popular Paths and exact Snapshot Date
    cursor.execute("""
        SELECT MAX(logged_date) FROM traffic_sources
        WHERE repo_id = ? AND source_type = 'path'
    """, (repo_id,))
    path_date_row = cursor.fetchone()
    paths_date = path_date_row[0] if path_date_row and path_date_row[0] else None

    paths = []
    if paths_date:
        cursor.execute("""
            SELECT source_or_path as path, count, uniques
            FROM traffic_sources
            WHERE repo_id = ? AND source_type = 'path' AND logged_date = ?
            ORDER BY count DESC
        """, (repo_id, paths_date))
        paths = [dict(row) for row in cursor.fetchall()]

    # 4. External Events (Updated to retrieve full metadata)
    event_query = "SELECT id, event_date, title, description, url, category FROM external_events WHERE repo_id = ?"
    e_params = [repo_id]
    if start_date:
        event_query += " AND event_date >= ?"
        e_params.append(start_date)
    if end_date:
        event_query += " AND event_date <= ?"
        e_params.append(end_date)
    event_query += " ORDER BY event_date ASC"

    cursor.execute(event_query, tuple(e_params))
    events = [dict(row) for row in cursor.fetchall()]

    conn.close()

    return {
        "summary": summary,
        "traffic": traffic,
        "referrers": referrers,
        "referrers_date": referrers_date,
        "paths": paths,
        "paths_date": paths_date,
        "events": events
    }

@app.post("/api/events")
def create_event(event: EventCreate):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO external_events (repo_id, event_date, title, description, url, category)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event.repo_id, event.event_date, event.title, event.description, event.url, event.category)
    )
    conn.commit()
    conn.close()
    return {"status": "success"}

# --- ROUTES FOR DETAILED SOURCE CHARTS ---

@app.get("/source/{repo_id}", response_class=HTMLResponse)
def read_source_detail(request: Request, repo_id: int, type: str, name: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT owner_repo FROM repositories WHERE id = ?", (repo_id,))
    repo = cursor.fetchone()
    conn.close()

    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    return templates.TemplateResponse(request=request, name="source_detail.html", context={
        "repo_id": repo_id,
        "repo_name": repo["owner_repo"],
        "source_type": type,
        "source_name": name
    })

@app.get("/api/metrics/{repo_id}/source_history")
def get_source_history(repo_id: int, type: str, name: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT logged_date, count as views, uniques
        FROM traffic_sources
        WHERE repo_id = ? AND source_type = ? AND source_or_path = ?
        ORDER BY logged_date ASC
    """, (repo_id, type, name))

    history = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return history

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)