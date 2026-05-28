"""
NexusQuery Enterprise Gateway — main.py (FastAPI Application)
Main entry point replacing app.py. Serves static files, mounts APIRouter,
handles cookie sessions, and renders Jinja2 template views asynchronously.
"""

import os
import time
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Response, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

# Core adapters and connection models
from core.validator import is_safe, classify_query
from core.snapshot import take_snapshot, undo, list_snapshots, self_heal_snapshots
from core.llm import generate_query_with_explanation
from core.connection_manager import (
    list_connections, add_connection, delete_connection as rem_connection,
    get_adapter_for_connection, ensure_default_sqlite,
)
from core.adapters import DB_DISPLAY_NAMES
from core import llm_manager

# System design components
from core.rate_limiter import global_rate_limiter
from core.circuit_breaker import cb_registry
from core.semantic_cache import global_semantic_cache

# FastAPI APIRouter
from api_routes_fastapi import router as api_router, get_active_db_info, get_active_adapter
from werkzeug.security import check_password_hash

app = FastAPI(
    title="NexusQuery Enterprise Query Gateway",
    description="FastAPI gateway managing natural-language database intelligence.",
    version="2.0.0"
)

# Cookie session middleware (uses Starlette secure cookies)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-replace-this-in-production")
)

# Ensure default SQLite database on startup and run storage self-healing to protect disk space
ensure_default_sqlite()
self_heal_snapshots()

# Mount Static assets
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Initialize Jinja2 HTML Templates
templates = Jinja2Templates(directory="templates")

# Register Asynchronous API Blueprint
app.include_router(api_router)

PAGE_SIZE = 50
REACT_DIST_INDEX = os.path.join(
    os.path.dirname(__file__), "meridian-frontend", "dist", "index.html"
)
SERVE_REACT_AT_ROOT = os.environ.get("SERVE_REACT_AT_ROOT", "1") == "1"
analysis_enabled = bool(os.environ.get("GROQ_API_KEY", ""))


# ---------------------------------------------------
# Concurrency Helper functions
# ---------------------------------------------------
def is_system_query(sql: str) -> bool:
    sql = sql.lower()
    return any(k in sql for k in ("sqlite_master", "pragma", "information_schema"))


def is_already_limited(sql: str) -> bool:
    sql = sql.lower()
    return " limit " in sql or " offset " in sql


def paginate_sql(sql: str, page: int) -> str:
    sql = sql.rstrip(";")
    offset = (page - 1) * PAGE_SIZE
    return f"{sql} LIMIT {PAGE_SIZE} OFFSET {offset}"


def safe_count(adapter, sql: str) -> Optional[int]:
    if is_system_query(sql) or is_already_limited(sql):
        return None
    try:
        _, rows = adapter.execute(f"SELECT COUNT(*) FROM ({sql.rstrip(';')}) AS subq")
        return rows[0][0] if rows else 0
    except Exception:
        return None


def rows_to_list(rows):
    return [list(r) for r in rows] if rows else []


def add_to_history(session: dict, query: str, sql: str, task: str, status: str):
    history = session.get("history", [])
    active_db = session.get("active_db", "Default SQLite")
    history.insert(0, {
        "query": query,
        "sql": sql,
        "task": task,
        "status": status,
        "user": session.get("username"),
        "db": active_db,
        "time": datetime.now().strftime("%H:%M")
    })
    session["history"] = history[:10]


# ---------------------------------------------------
# Global Middleware to enforce session check
# ---------------------------------------------------
@app.middleware("http")
async def require_login_middleware(request: Request, call_next):
    path = request.url.path
    
    # Exclusions
    if (
        path.startswith("/login") or 
        path.startswith("/static") or 
        path.startswith("/assets") or 
        path == "/favicon.ico" or
        path.startswith("/api/auth/login") or
        path.startswith("/api/auth/session") or
        path == "/app" or
        path.startswith("/app/")
    ):
        return await call_next(request)
        
    session = request.scope.get("session", {})
    if not session.get("logged_in"):
        if path.startswith("/api/"):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return RedirectResponse(url="/login")
        
    return await call_next(request)


# ---------------------------------------------------
# UI Routes Serving Jinja2 HTML Templates
# ---------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    username: str = Form(""),
    password: str = Form("")
):
    from api_routes_fastapi import USERS
    user = USERS.get(username)
    if not user or not check_password_hash(user["password"], password):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})

    session = request.scope.get("session", {})
    session.clear()
    session["logged_in"] = True
    session["username"] = username
    session["role"] = user["role"]
    session["active_db"] = "Default SQLite"
    
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
async def logout(request: Request):
    session = request.scope.get("session", {})
    session.clear()
    return RedirectResponse(url="/login")


@app.get("/", response_class=HTMLResponse)
async def index_get(request: Request):
    if SERVE_REACT_AT_ROOT and os.path.exists(REACT_DIST_INDEX):
        return RedirectResponse(url="/app")
        
    session = request.scope.get("session", {})
    db_info = get_active_db_info(request)
    conns = list_connections()
    llm_provider = session.get("llm_provider", "mistral")
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "db_info": db_info,
        "connections": conns,
        "llm_provider": llm_provider,
        "history": session.get("history", []),
        "analysis_enabled": analysis_enabled
    })


@app.post("/", response_class=HTMLResponse)
async def index_post(
    request: Request,
    command: str = Form("")
):
    session = request.scope.get("session", {})
    db_info = get_active_db_info(request)
    conns = list_connections()
    llm_provider = session.get("llm_provider", "mistral")
    role = session.get("role", "VIEWER")

    user_cmd = command.strip()
    if not user_cmd:
        return templates.TemplateResponse("index.html", {
            "request": request, "error": "Empty command.",
            "db_info": db_info, "connections": conns, "llm_provider": llm_provider,
            "history": session.get("history", []), "analysis_enabled": analysis_enabled
        })

    # 1. Distributed Rate Limiting Check
    client_ip = request.client.host if request.client else "unknown"
    if not global_rate_limiter.consume(client_ip):
        return templates.TemplateResponse("index.html", {
            "request": request, "error": "Too many requests. Rate limit exceeded (Token Bucket).",
            "db_info": db_info, "connections": conns, "llm_provider": llm_provider,
            "history": session.get("history", []), "analysis_enabled": analysis_enabled
        })

    adapter = get_active_adapter(request)
    dialect = adapter.dialect
    cmd_lower = user_cmd.lower().strip()

    # --- Hardcoded: DESCRIBE TABLE ---
    describe_match = None
    for prefix in ("describe ", "desc ", "show structure ", "show columns ", "show schema "):
        if cmd_lower.startswith(prefix):
            describe_match = user_cmd[len(prefix):].strip().strip(";").strip('"').strip("'")
            break

    if describe_match and hasattr(adapter, 'describe_table'):
        try:
            info = await asyncio.to_thread(adapter.describe_table, describe_match)
            columns = ["Column", "Type", "Not Null", "Default", "Primary Key"]
            rows = []
            for c in info["columns"]:
                rows.append([c["name"], c["type"], "YES" if c["not_null"] else "NO",
                             c["default"] if c["default"] is not None else "", "YES" if c["primary_key"] else "NO"])

            session["last_read_sql"] = f'DESCRIBE "{describe_match}"'
            session["last_query"] = user_cmd
            session["last_explanation"] = f"Structure of table '{describe_match}'."
            session["last_read_columns"] = columns
            add_to_history(session, user_cmd, f"DESCRIBE {describe_match}", "SYSTEM", "EXECUTED")

            return templates.TemplateResponse("index.html", {
                "request": request, "task": "SYSTEM", "columns": columns, "results": rows,
                "page": 1, "page_size": len(rows), "total_rows": len(rows), "sql": f'DESCRIBE "{describe_match}"',
                "explanation": f"Table '{describe_match}': {info['row_count']} rows",
                "history": session.get("history", []), "db_info": db_info, "connections": conns,
                "llm_provider": llm_provider, "analysis_enabled": analysis_enabled
            })
        except Exception as e:
            return templates.TemplateResponse("index.html", {
                "request": request, "error": f"Table '{describe_match}' not found: {str(e)}",
                "db_info": db_info, "connections": conns, "llm_provider": llm_provider,
                "history": session.get("history", []), "analysis_enabled": analysis_enabled
            })

    # --- SHOW TABLES ---
    if cmd_lower in ("list tables", "show tables", "list collections", "show collections"):
        tables = await asyncio.to_thread(adapter.list_tables)
        label = "Collections" if adapter.is_nosql else "Tables"
        session["last_read_sql"] = "SHOW TABLES"
        session["last_read_columns"] = [label]
        add_to_history(session, user_cmd, "SHOW TABLES", "SYSTEM", "EXECUTED")

        return templates.TemplateResponse("index.html", {
            "request": request, "task": "SYSTEM", "columns": [label], "results": [[t] for t in tables],
            "page": 1, "page_size": len(tables), "total_rows": len(tables),
            "history": session.get("history", []), "db_info": db_info, "connections": conns,
            "llm_provider": llm_provider, "analysis_enabled": analysis_enabled
        })

    # --- LLM Async Text-to-SQL Query Generation (with Semantic Caching & Circuit Breaker) ---
    schema = await asyncio.to_thread(adapter.get_schema)
    conversation_context = session.get("conversation_context", [])

    # 1. Semantic Cache Lookup
    cached_query = global_semantic_cache.lookup(user_cmd, dialect)
    if cached_query:
        query = cached_query
        explanation = "Retrieved instantly from enterprise semantic vector cache (0ms LLM latency)."
    else:
        # 2. Resilient Circuit Breaker Wrapped Generation
        llm_breaker = cb_registry.get_breaker(f"llm_{llm_provider}", failure_threshold=3, recovery_timeout=15.0)
        try:
            query, explanation = await asyncio.to_thread(
                llm_breaker.call,
                generate_query_with_explanation,
                user_cmd, dialect, schema, llm_provider, history=conversation_context
            )
            global_semantic_cache.store(user_cmd, query, dialect)
        except Exception as e:
            if "circuit breaker" in str(e).lower() or "open" in str(e).lower():
                return templates.TemplateResponse("index.html", {
                    "request": request, "error": f"Circuit Breaker [{llm_breaker.state}] is active. Service down. Fast-failing.",
                    "db_info": db_info, "connections": conns, "llm_provider": llm_provider,
                    "history": session.get("history", []), "analysis_enabled": analysis_enabled
                })
            return templates.TemplateResponse("index.html", {
                "request": request, "error": f"LLM translation failed: {str(e)}",
                "db_info": db_info, "connections": conns, "llm_provider": llm_provider,
                "history": session.get("history", []), "analysis_enabled": analysis_enabled
            })

    conversation_context.append({"user": user_cmd, "assistant": query})
    if len(conversation_context) > 5:
        conversation_context.pop(0)
    session["conversation_context"] = conversation_context

    task = classify_query(query, dialect)
    safe_check = is_safe(query, dialect)

    effective_task = task
    from api_routes_fastapi import is_allowed
    if task == "UNKNOWN" and role in ("ADMIN", "EDITOR") and safe_check:
        effective_task = "READ"

    if not is_allowed(role, effective_task):
        add_to_history(session, user_cmd, query, task, "BLOCKED")
        return templates.TemplateResponse("index.html", {
            "request": request, "error": f"{role} not allowed to run {task}",
            "db_info": db_info, "connections": conns, "llm_provider": llm_provider,
            "history": session.get("history", []), "analysis_enabled": analysis_enabled
        })

    # READ / SYSTEM execution
    if task in ("READ", "SYSTEM") or (task == "UNKNOWN" and effective_task == "READ"):
        if not is_safe(query, dialect):
            add_to_history(session, user_cmd, query, "READ", "BLOCKED")
            return templates.TemplateResponse("index.html", {
                "request": request, "error": "Unsafe query blocked.",
                "db_info": db_info, "connections": conns, "llm_provider": llm_provider,
                "history": session.get("history", []), "analysis_enabled": analysis_enabled
            })

        session["last_read_sql"] = query
        session["last_explanation"] = explanation
        session["last_query"] = user_cmd
        page = 1

        try:
            if adapter.is_nosql:
                columns, rows = await asyncio.to_thread(adapter.execute, query)
                total_rows = len(rows)
                paginated_sql = query
            elif is_system_query(query) or is_already_limited(query):
                paginated_sql = query
                columns, rows = await asyncio.to_thread(adapter.execute, query)
                total_rows = len(rows)
            else:
                paginated_sql = paginate_sql(query, page)
                columns, rows = await asyncio.to_thread(adapter.execute, paginated_sql)
                total_rows = await asyncio.to_thread(safe_count, adapter, query)
        except Exception as e:
            return templates.TemplateResponse("index.html", {
                "request": request, "sql": query, "explanation": explanation, "task": "READ", "error": f"Execution failed: {str(e)}",
                "db_info": db_info, "connections": conns, "llm_provider": llm_provider,
                "history": session.get("history", []), "analysis_enabled": analysis_enabled
            })

        session["last_read_columns"] = columns
        add_to_history(session, user_cmd, query, task, "EXECUTED")

        return templates.TemplateResponse("index.html", {
            "request": request, "sql": paginated_sql, "explanation": explanation, "task": task,
            "columns": columns,
            "results": rows_to_list(rows) if rows and not isinstance(rows[0], list) else rows if rows else [],
            "page": page, "page_size": PAGE_SIZE, "total_rows": total_rows,
            "history": session.get("history", []), "db_info": db_info, "connections": conns,
            "llm_provider": llm_provider, "analysis_enabled": analysis_enabled
        })

    # WRITE / SCHEMA -> held in review
    add_to_history(session, user_cmd, query, task, "PENDING REVIEW")
    session["last_sql"] = query
    session["last_task"] = task
    session["last_explanation"] = explanation

    return templates.TemplateResponse("review.html", {
        "request": request, "sql": query, "explanation": explanation, "task": task,
        "history": session.get("history", []), "db_info": db_info, "connections": conns
    })


@app.get("/review", response_class=HTMLResponse)
async def review_get(request: Request):
    session = request.scope.get("session", {})
    db_info = get_active_db_info(request)
    conns = list_connections()
    return templates.TemplateResponse("review.html", {
        "request": request,
        "sql": session.get("last_sql"),
        "explanation": session.get("last_explanation"),
        "task": session.get("last_task"),
        "history": session.get("history", []),
        "db_info": db_info,
        "connections": conns
    })


@app.get("/metrics", response_class=HTMLResponse)
async def metrics_view(request: Request):
    """
    Renders the live system design telemetry dashboard.
    """
    session = request.scope.get("session", {})
    db_info = get_active_db_info(request)
    conns = list_connections()
    return templates.TemplateResponse("insights.html", {
        "request": request,
        "history": session.get("history", []),
        "db_info": db_info,
        "connections": conns,
        "analysis_enabled": analysis_enabled
    })


# ---------------------------------------------------
# Serve React Build Index Route
# ---------------------------------------------------
@app.get('/app', response_class=HTMLResponse)
@app.get('/app/{path:path}', response_class=HTMLResponse)
async def serve_react(request: Request, path: str = ''):
    dist_dir = os.path.join(os.path.dirname(__file__), 'meridian-frontend', 'dist')
    if path and os.path.exists(os.path.join(dist_dir, path)):
        return FileResponse(os.path.join(dist_dir, path))
    index = os.path.join(dist_dir, 'index.html')
    if os.path.exists(index):
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="React build not found. Run: cd meridian-frontend && npm run build")


# Run Uvicorn Development Server
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=5000, reload=True)
