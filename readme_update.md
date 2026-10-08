# GitHub Traffic Analytics & Historical Data Tracker

[![GitHub License](https://shields.io)](https://github.com)
[![GitHub Stars](https://shields.io)](https://github.com)

**GitHub-Traffic-Analytics** is a local-first Python automation tool, FastAPI web dashboard, and Model Context Protocol (MCP) server engineered to bypass GitHub's strict 14-day history limit. It automatically backs up and analyzes your repository's traffic metrics into a local SQLite database.

Visit the official [GitHub Traffic Analytics Repository](https://github.com/KikeVen/GitHub-Traffic-Analytics) for full documentation and code.

---

## 🗺️ Table of Contents
* [GitHub Traffic Analytics \& Historical Data Tracker](#github-traffic-analytics--historical-data-tracker)
  * [🗺️ Table of Contents](#️-table-of-contents)
  * [🚀 Key Features](#-key-features)
  * [🏗️ System Architecture](#️-system-architecture)
  * [📋 Prerequisites \& Installation](#-prerequisites--installation)
  * [🔌 MCP Server Integration \& Usage](#-mcp-server-integration--usage)
  * [📄 License](#-license)

---

## 🚀 Key Features

* **Bypass the 14-Day Limit:** Permanently save historical repository metrics via automated data ingestion.
* **Web UI & Visualization:** Filter and analyze traffic trends with an interactive dashboard and external event logging.
* **Built-in MCP Server:** Allow AI agents to query your analytics database directly.

---

## 🏗️ System Architecture

![GitHub Traffic Analytics system architecture diagram showcasing GitHub CLI data fetching, SQLite database persistence, FastAPI web rendering, and MCP server outputs](img/architecture_placeholder.png)

* **ingester.py:** Coordinates GitHub API requests and manages database persistence.
* **main.py & templates/:** Powers the local FastAPI server and web dashboard.
* **mcp_server.py:** Exposes structured analytic tools to connected LLMs.

---

## 📋 Prerequisites & Installation

Requires **Python 3.10+** and authenticated **GitHub CLI (`gh`)**:
```bash
gh auth login
```

1. **Clone and setup:**
   ```bash
   git clone https://github.com.git
   cd GitHub-Traffic-Analytics
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **Run sync and start dashboard:**
   ```bash
   python ingester.py
   python main.py
   ```
   Open **http://localhost:8000** in your browser.

---

## 🔌 MCP Server Integration & Usage

Configure your local `mcp.json` for Claude Desktop to utilize available developer tools like `trigger_ingester`, `get_repo_metrics`, and `query_sql`.

---

## 📄 License

Distributed under the **MIT License**.
