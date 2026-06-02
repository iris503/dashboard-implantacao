#!/usr/bin/env python3
"""
Dashboard Implantação WMI — FastAPI Application
Imports all business logic from generate_dashboard_v3.py (single source of truth).
Serves a live dashboard with auto-refresh from Jira.
"""

import os
import sys
import asyncio
import logging
from typing import Dict

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

# ─── Import business logic from the working script ─────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))
from generate_dashboard_v3 import JiraClient, generate_dashboard_data

# ─── Logging ───────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dashboard")

# ─── Configuration ─────────────────────────────────────────
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://wmi-solutions.atlassian.net")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "1800"))  # 30 min default

# In-memory cache
_dashboard_cache: Dict = {}
_cache_lock = asyncio.Lock()


# ─── Background refresh task ──────────────────────────────
async def refresh_data():
    """Fetch fresh data from Jira and update cache."""
    global _dashboard_cache
    try:
        if not all([JIRA_EMAIL, JIRA_API_TOKEN]):
            logger.warning("Jira credentials not set — skipping refresh")
            return
        client = JiraClient(JIRA_EMAIL, JIRA_API_TOKEN, JIRA_BASE_URL)
        epics = await asyncio.to_thread(client.get_epics)
        data = await asyncio.to_thread(generate_dashboard_data, epics)
        async with _cache_lock:
            _dashboard_cache = data
        logger.info(f"Cache updated: {len(epics)} epics, {len(data.get('technicians', []))} technicians")
    except Exception as e:
        logger.error(f"Refresh failed: {e}", exc_info=True)


async def periodic_refresh():
    """Run refresh_data every REFRESH_INTERVAL seconds."""
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        await refresh_data()


# ─── FastAPI Lifespan ─────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await refresh_data()
    task = asyncio.create_task(periodic_refresh())
    yield
    task.cancel()


app = FastAPI(title="Dashboard Implantação WMI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Routes ───────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/data")
async def api_data():
    async with _cache_lock:
        if not _dashboard_cache:
            return JSONResponse({"error": "Data not loaded yet"}, status_code=200)
        return _dashboard_cache


@app.get("/api/refresh")
async def api_refresh():
    await refresh_data()
    return {"status": "refreshed"}


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()
