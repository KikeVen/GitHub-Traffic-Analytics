---
name: github-analytics-skill
description: >
  Multi-dimensional open-source repository analytics using the `github-analytics` MCP.
  Trigger this skill whenever the user asks to analyze, review, or pull stats for a GitHub
  repository — including traffic, stars, clones, referrers, code churn, events, or CI/CD
  anomalies. Always trigger when the user mentions "github-analytics", "repo stats",
  "traffic data", "clone spikes", "star history", "referrer sources", or any variant of
  "what's happening with my repo". Never answer from cached data — always run
  `trigger_ingester` first before any query. Use this skill even for partial requests like
  "how many stars" or "what happened on [date]".
compatibility:
  tools:
    - github-analytics MCP (required)
    - Tavily MCP (optional — for contextual event validation)
---

# GitHub Analytics Skill

Multi-dimensional open-source analytics for any repository tracked in the `github-analytics`
MCP. Isolates machine loops, maps developer paths, correlates external events, and surfaces
actionable signals — always from fresh data.

---

## Step 0 — ALWAYS Run Ingester First

**Never query stale data.** Before any other tool call:

```
mcp__github-analytics__trigger_ingester(repo="owner/repo_name")
```

Confirm success before proceeding.

---

## Step 1 — Discover Tracked Repos

If the repo name is ambiguous or unknown:

```sql
SELECT id, owner_repo, added_at FROM repositories ORDER BY added_at DESC
```

Confirm `repo_id` before using it in subsequent queries.

---

## Step 2 — Available Tools

The MCP exposes these tools. Use all that are relevant before synthesizing:

| Tool | When to use |
|------|-------------|
| `trigger_ingester` | **Always first.** Pulls fresh GitHub data into the DB. |
| `get_repo_metrics` | Daily views + clones for a date range. |
| `list_events` | Logged external events (launches, posts, releases). |
| `query_sql` | Raw SELECT on any table for custom analysis. |
| `log_external_event` | Log a newly discovered event the user confirms. |
| `update_external_event` | Correct or enrich an existing event record. |
| `delete_external_event` | Remove a bad or duplicate event record. |

---

## Step 3 — Core Query Templates

### 3a. Bot/CI Spike Detection

Separates organic developer clones from automated machine loops (ratio ≥ 8.0):

```sql
SELECT
    date,
    views_count,
    views_uniques,
    clones_count AS raw_clones_count,
    clones_uniques,
    CASE WHEN (clones_count * 1.0) / NULLIF(clones_uniques, 0) >= 8.0
         THEN 1 ELSE 0 END AS bot_flag,
    CASE WHEN (clones_count * 1.0) / NULLIF(clones_uniques, 0) >= 8.0
         THEN clones_uniques ELSE clones_count END AS clean_clones_count,
    ROUND((clones_count * 1.0) / NULLIF(clones_uniques, 0), 2) AS clone_ratio
FROM daily_traffic
WHERE repo_id = (SELECT id FROM repositories WHERE owner_repo = :repo_name)
ORDER BY date ASC;
```

Flag any day where `clone_ratio > 8.0` as likely CI/CD or automated pipeline activity.
Note: a high ratio does NOT require a push from the repo owner — pipelines clone on their
own schedule.

### 3b. Deep-Funnel Path Conversion

Maps how far developers browse into the repo after landing:

```sql
SELECT
    source_or_path AS path_target,
    SUM(count) AS total_views,
    SUM(uniques) AS unique_visitors
FROM traffic_sources
WHERE repo_id = (SELECT id FROM repositories WHERE owner_repo = :repo_name)
  AND source_type = 'path'
  AND (source_or_path LIKE '%/pulse%'
       OR source_or_path LIKE '%/releases%'
       OR source_or_path LIKE '%/issues%'
       OR source_or_path LIKE '%/discussions%'
       OR source_or_path LIKE '%/pulls%'
       OR source_or_path LIKE '%/graphs/traffic%'
       OR source_or_path LIKE '%/blob/%'
       OR source_or_path LIKE '%/network/dependencies%')
GROUP BY source_or_path
ORDER BY total_views DESC;
```

Interpret path engagement:
- `/issues`, `/discussions`, `/pulls` → community engagement, not just browsing
- `/network/dependencies` → due diligence before adoption
- `/blob/main/...` → reading specific files (note which ones)
- `/graphs/traffic`, `/pulse` → likely the repo owner monitoring stats

### 3c. Normalized Referrer Ecosystem

Aggregates fragmented referrer sub-domains into clean buckets:

```sql
SELECT
    CASE
        WHEN source_or_path LIKE '%reddit%'       THEN 'Reddit'
        WHEN source_or_path LIKE '%github%'        THEN 'GitHub Internal'
        WHEN source_or_path LIKE '%google%'        THEN 'Google Search'
        WHEN source_or_path LIKE '%ycombinator%'   THEN 'Hacker News'
        WHEN source_or_path LIKE '%kagi%'          THEN 'Kagi Search'
        ELSE source_or_path
    END AS normalized_source,
    SUM(count)   AS total_clicks,
    SUM(uniques) AS unique_referral_visitors
FROM traffic_sources
WHERE repo_id = (SELECT id FROM repositories WHERE owner_repo = :repo_name)
  AND source_type = 'referrer'
GROUP BY normalized_source
ORDER BY total_clicks DESC;
```

### 3d. Star History (Daily + Cumulative)

```sql
SELECT
    DATE(starred_at) AS day,
    COUNT(*)         AS daily_stars,
    SUM(COUNT(*)) OVER (ORDER BY DATE(starred_at)) AS cumulative_stars
FROM stars_history
WHERE repo_id = (SELECT id FROM repositories WHERE owner_repo = :repo_name)
GROUP BY day
ORDER BY day;
```

### 3e. Weekly Code Churn

```sql
SELECT week_timestamp, additions, ABS(deletions) AS deletions
FROM community_stats
WHERE repo_id = (SELECT id FROM repositories WHERE owner_repo = :repo_name)
ORDER BY week_timestamp;
```

---

## Step 4 — Contextual Event Validation

When a traffic spike is identified with no matching event in `external_events`:

1. Query `external_events` for the date range around the spike.
2. If no event found, use **Tavily** to search for mentions:
   - Query: `"[repo name]" OR "[owner] [repo name]"` bounded to the spike date ± 3 days.
3. If an external mention is found, present it to the user and ask:
   > "Want me to log this into `external_events`?"
4. If confirmed, use `log_external_event` to persist it.

---

## Step 5 — Synthesis & Output

After all queries, synthesize into:

- **KPIs**: total views, total clones (clean), total stars, peak day
- **Anomalies**: any `bot_flag = 1` days with ratio and likely cause
- **Engagement signals**: which paths beyond the homepage were visited, by how many uniques
- **Referrer story**: where traffic is coming from and quality (uniques ratio)
- **Star velocity**: burst vs slow drip, correlation to events
- **Code churn narrative**: active build phases vs quiet periods
- **Event correlation**: what external actions drove what spikes

When the user asks for visuals, build an HTML dashboard artifact using D3.js from
`cdnjs.cloudflare.com`. Dark theme matching GitHub's palette (`#0d1117` bg, `#58a6ff`
accent, `#3fb950` green, `#d29922` yellow). Always include: traffic line chart with event
overlays, cumulative stars chart, referrer bar chart, churn bar chart, and event timeline.

---

## Schema Reference

```
repositories     — id, owner_repo, is_active, added_at
daily_traffic    — repo_id, date, views_count, views_uniques, clones_count, clones_uniques
traffic_sources  — repo_id, logged_date, source_type (referrer|path), source_or_path, title, count, uniques
stars_history    — repo_id, user_handle, starred_at
community_stats  — repo_id, week_timestamp, additions, deletions, commits
external_events  — id, repo_id, event_date, title, description, url, category
```

---

## Rules

- **Never skip `trigger_ingester`.** Stale data is worthless.
- **Never fabricate** event context — validate via `external_events` or Tavily.
- **Never expose user handles** from `stars_history` without explicit user request.
- Bot-flagged clone days should always be noted but **not counted** in organic totals.
- `/graphs/traffic` and `/pulse` path hits with `uniques = 1` are almost always the repo
  owner — exclude from engagement interpretation.