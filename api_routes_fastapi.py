"""
FastAPI APIRouter for the NexusQuery React Frontend.
Provides high-performance, asynchronous REST controllers, Pydantic validations,
and integrated system design features (rate limiters, circuit breakers, semantic cache).
"""

import os
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Request, Response, Depends, HTTPException, status
from pydantic import BaseModel, Field

# Core managers and adapters
from core.validator import is_safe, classify_query
from core.snapshot import (
    take_snapshot, undo, list_snapshots, delete_snapshot, restore_snapshot
)
from core.llm import generate_query_with_explanation
from core.connection_manager import (
    list_connections, add_connection, delete_connection as rem_connection,
    get_adapter_for_connection, test_connection, test_new_connection,
)
from core.adapters import DB_TYPES, DB_DISPLAY_NAMES, DB_CONNECTION_FIELDS
from core.metrics import get_summary
from core.dashboards import list_dashboards, get_dashboard
from core import llm_manager

# System design components
from core.rate_limiter import global_rate_limiter
from core.circuit_breaker import cb_registry
from core.semantic_cache import global_semantic_cache

# Werkzeug security for legacy user database
from werkzeug.security import generate_password_hash, check_password_hash

router = APIRouter(prefix="/api")

USERS = {
    "viewer1": {"password": generate_password_hash("viewer123"), "role": "VIEWER"},
    "editor1": {"password": generate_password_hash("editor123"), "role": "EDITOR"},
    "admin1":  {"password": generate_password_hash("admin123"),  "role": "ADMIN"},
}

ROLE_PERMISSIONS = {
    "VIEWER": {"READ", "SYSTEM"},
    "EDITOR": {"READ", "WRITE", "SYSTEM"},
    "ADMIN":  {"READ", "WRITE", "SCHEMA", "SYSTEM"},
}

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
analysis_enabled = bool(GROQ_API_KEY)
PAGE_SIZE = 50


# ---------------------------------------------------
# Pydantic Schemas for Request & Response Validation
# ---------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(..., examples=["admin1"])
    password: str = Field(..., examples=["admin123"])


class ConnectionConfig(BaseModel):
    name: str
    db_type: str = Field(default="sqlite")
    config: Dict[str, Any] = Field(default_factory=dict)


class SelectConnectionRequest(BaseModel):
    name: str


class CommandRequest(BaseModel):
    command: str = Field(..., min_length=1, examples=["show tables"])


class ExecuteRequest(BaseModel):
    sql: Optional[str] = None


class SetProviderRequest(BaseModel):
    provider: str = Field(..., pattern="^(mistral|groq)$")


class RestoreSnapshotRequest(BaseModel):
    snap_id: str
    connection_name: str


class CreateDatabaseRequest(BaseModel):
    db_name: str
    db_type: str = Field(default="sqlite")
    tables: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------
# Dependency Injection Checks
# ---------------------------------------------------
async def rate_limit_guard(request: Request):
    """Enforces Token Bucket rate limiting on critical request headers."""
    client_ip = request.client.host if request.client else "unknown"
    if not global_rate_limiter.consume(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Rate limit exceeded (Token Bucket)."
        )


def get_current_user_role(request: Request) -> str:
    """Helper to retrieve user role from session."""
    session = request.scope.get("session", {})
    if not session.get("logged_in"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or unauthorized."
        )
    return session.get("role", "VIEWER")


def get_active_db_info(request: Request) -> dict:
    session = request.scope.get("session", {})
    name = session.get("active_db", "Default SQLite")
    connections = list_connections()
    for conn in connections:
        if conn["name"] == name:
            return {
                "name": name,
                "db_type": conn["db_type"],
                "display_type": DB_DISPLAY_NAMES.get(conn["db_type"], conn["db_type"]),
                "is_nosql": conn["db_type"] in ("mongodb", "redis"),
                "supports_snapshot": conn["db_type"] == "sqlite",
            }
    return {"name": name, "db_type": "sqlite", "display_type": "SQLite", "is_nosql": False, "supports_snapshot": True}


def get_active_adapter(request: Request):
    session = request.scope.get("session", {})
    name = session.get("active_db", "Default SQLite")
    try:
        return get_adapter_for_connection(name)
    except Exception:
        session["active_db"] = "Default SQLite"
        return get_adapter_for_connection("Default SQLite")


def is_allowed(role: str, task: str) -> bool:
    return task in ROLE_PERMISSIONS.get(role, set())


def rows_to_list(rows) -> list:
    return [list(r) for r in rows] if rows else []


def is_system_query(sql: str) -> bool:
    sql_l = sql.lower()
    return any(k in sql_l for k in ("sqlite_master", "pragma", "information_schema"))


def is_already_limited(sql: str) -> bool:
    sql_l = sql.lower()
    return " limit " in sql_l or " offset " in sql_l


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


# ---------------------------------------------------
# Route Controllers
# ---------------------------------------------------

@router.post('/auth/login')
async def api_login(request: Request, payload: LoginRequest):
    user = USERS.get(payload.username)
    if not user or not check_password_hash(user["password"], payload.password):
        return {"success": False, "error": "Invalid credentials"}

    session = request.scope.get("session", {})
    session.clear()
    session["logged_in"] = True
    session["username"] = payload.username
    session["role"] = user["role"]
    session["active_db"] = "Default SQLite"

    return {"success": True, "username": payload.username, "role": user["role"]}


@router.post('/auth/logout')
async def api_logout(request: Request):
    session = request.scope.get("session", {})
    session.clear()
    return {"success": True}


@router.get('/auth/session')
async def api_session(request: Request):
    session = request.scope.get("session", {})
    if session.get("logged_in"):
        return {
            "logged_in": True,
            "username": session.get("username"),
            "role": session.get("role"),
        }
    raise HTTPException(status_code=401, detail="Unauthorized")


@router.get('/connections')
async def api_connections(request: Request):
    session = request.scope.get("session", {})
    conns = list_connections()
    return {
        "connections": conns,
        "active_db": session.get("active_db", "Default SQLite"),
        "db_info": get_active_db_info(request),
        "llm_provider": session.get("llm_provider", "mistral"),
    }


@router.post('/connections')
async def api_add_connection(payload: ConnectionConfig):
    name = payload.name.strip()
    if not name:
        return {"success": False, "message": "Connection name cannot be blank"}
    # Run blocking config files writes in a separate executor thread
    result = await asyncio.to_thread(add_connection, name, payload.db_type, payload.config)
    return result


@router.delete('/connections/{name}')
async def api_delete_connection(request: Request, name: str):
    session = request.scope.get("session", {})
    if name == "Default SQLite":
        return {"success": False, "message": "Cannot delete default connection"}
    if session.get("active_db") == name:
        session["active_db"] = "Default SQLite"
    
    result = await asyncio.to_thread(rem_connection, name)
    return result


@router.post('/connections/select')
async def api_select_connection(request: Request, payload: SelectConnectionRequest):
    session = request.scope.get("session", {})
    conns = list_connections()
    names = [c["name"] for c in conns]

    if payload.name in names:
        session["active_db"] = payload.name
        session.pop("last_read_sql", None)
        session.pop("last_read_columns", None)
        session.pop("last_sql", None)
        session.pop("last_task", None)
        session.pop("last_explanation", None)
        session.pop("conversation_context", None)
        return {"success": True, "db_info": get_active_db_info(request)}
    
    raise HTTPException(status_code=400, detail=f"Connection '{payload.name}' not found")


@router.get('/db-types')
async def api_db_types():
    return {
        "db_types": DB_TYPES,
        "db_display_names": DB_DISPLAY_NAMES,
        "db_fields": DB_CONNECTION_FIELDS,
    }


# ---------------------------------------------------
# Command Execution API (Rate-Limited)
# ---------------------------------------------------
@router.post('/command', dependencies=[Depends(rate_limit_guard)])
async def api_command(
    request: Request,
    payload: CommandRequest,
    role: str = Depends(get_current_user_role),
    adapter = Depends(get_active_adapter)
):
    user_cmd = payload.command.strip()
    dialect = adapter.dialect
    cmd_lower = user_cmd.lower().strip()
    session = request.scope.get("session", {})

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
            if info["foreign_keys"]:
                rows.append(["", "", "", "", ""])
                rows.append(["--- FOREIGN KEYS ---", "", "", "", ""])
                for fk in info["foreign_keys"]:
                    rows.append([fk["from"], f"-> {fk['to_table']}.{fk['to_column']}", "", "", ""])
            if info["indexes"]:
                rows.append(["", "", "", "", ""])
                rows.append(["--- INDEXES ---", "", "", "", ""])
                for idx in info["indexes"]:
                    rows.append([idx["name"], ", ".join(idx["columns"]), "UNIQUE" if idx["unique"] else "", "", ""])

            session["last_read_sql"] = f'DESCRIBE "{describe_match}"'
            session["last_read_columns"] = columns
            return {"task": "SYSTEM", "columns": columns, "results": rows, "sql": f'DESCRIBE "{describe_match}"',
                    "explanation": f"Table '{describe_match}': {info['row_count']} rows", "total_rows": len(rows), "page": 1, "page_size": len(rows)}
        except Exception as e:
            return {"error": f"Table '{describe_match}' not found: {str(e)}"}

    # --- Hardcoded: SHOW FOREIGN KEYS ---
    if cmd_lower in ("show foreign keys", "list foreign keys", "show fk", "show fks",
                      "show relationships", "list relationships", "show refs", "show references"):
        if hasattr(adapter, 'get_foreign_keys'):
            fks = await asyncio.to_thread(adapter.get_foreign_keys)
            columns = ["From Table", "From Column", "To Table", "To Column"]
            rows = [[fk["from_table"], fk["from_column"], fk["to_table"], fk["to_column"]] for fk in fks]
            if not rows:
                rows = [["No foreign keys found", "", "", ""]]
            session["last_read_sql"] = "-- Foreign Key Relationships"
            session["last_read_columns"] = columns
            return {"task": "SYSTEM", "columns": columns, "results": rows, "total_rows": len(rows), "page": 1, "page_size": len(rows),
                    "explanation": f"Found {len(fks)} foreign key relationships."}

    # --- TABLE ROW COUNTS ---
    if cmd_lower in ("show table counts", "show row counts", "count all tables", "table sizes"):
        try:
            tables = await asyncio.to_thread(adapter.list_tables)
            columns = ["Table Name", "Row Count"]
            rows = []
            for t in tables:
                try:
                    _, cr = await asyncio.to_thread(adapter.execute, f'SELECT COUNT(*) FROM "{t}"')
                    rows.append([t, cr[0][0] if cr else 0])
                except Exception:
                    rows.append([t, "Error"])
            session["last_read_sql"] = "-- Table Row Counts"
            session["last_read_columns"] = columns
            return {"task": "SYSTEM", "columns": columns, "results": rows, "total_rows": len(rows), "page": 1, "page_size": len(rows),
                    "explanation": f"Row counts for {len(tables)} tables."}
        except Exception as e:
            return {"error": str(e)}

    # --- LIST TABLES ---
    if cmd_lower in ("list tables", "show tables", "list collections", "show collections"):
        if not is_allowed(role, "SYSTEM"):
            return {"error": "Permission denied."}
        tables = await asyncio.to_thread(adapter.list_tables)
        label = "Collections" if adapter.is_nosql else "Tables"
        session["last_read_sql"] = "SHOW TABLES"
        session["last_read_columns"] = [label]
        return {"task": "SYSTEM", "columns": [label], "results": [[t] for t in tables],
                "total_rows": len(tables), "page": 1, "page_size": len(tables)}

    # --- LLM Query Generation (Asynchronous & Resilient) ---
    schema = await asyncio.to_thread(adapter.get_schema)
    conversation_context = session.get("conversation_context", [])
    llm_provider = session.get("llm_provider", "mistral")

    # 1. Semantic Cache Lookup
    cached_query = global_semantic_cache.lookup(user_cmd, dialect)
    if cached_query:
        query = cached_query
        explanation = "Retrieved instantly from enterprise semantic vector cache (0ms LLM latency)."
    else:
        # 2. Resilient Circuit Breaker Wrapped Generation
        llm_breaker = cb_registry.get_breaker(f"llm_{llm_provider}", failure_threshold=3, recovery_timeout=15.0)
        try:
            # Delegate blocking HTTP call to thread pool
            query, explanation = await asyncio.to_thread(
                llm_breaker.call,
                generate_query_with_explanation,
                user_cmd, dialect, schema, llm_provider, history=conversation_context
            )
            # Store back in cache on success
            global_semantic_cache.store(user_cmd, query, dialect)
        except Exception as e:
            if "circuit breaker" in str(e).lower() or "open" in str(e).lower():
                raise HTTPException(
                    status_code=503,
                    detail=f"Circuit Breaker [{llm_breaker.state}] is active. LLM service is down. Fast-failing."
                )
            raise HTTPException(status_code=500, detail=f"LLM translation failed: {str(e)}")

    conversation_context.append({"user": user_cmd, "assistant": query})
    if len(conversation_context) > 5:
        conversation_context.pop(0)
    session["conversation_context"] = conversation_context

    task = classify_query(query, dialect)
    safe_check = is_safe(query, dialect)

    effective_task = task
    if task == "UNKNOWN" and role in ("ADMIN", "EDITOR") and safe_check:
        effective_task = "READ"

    if not is_allowed(role, effective_task):
        return {"error": f"{role} not allowed to run {task}"}

    # READ / SYSTEM direct execution
    if task in ("READ", "SYSTEM") or (task == "UNKNOWN" and effective_task == "READ"):
        if not is_safe(query, dialect):
            return {"error": "Unsafe query blocked."}

        session["last_read_sql"] = query
        session["last_explanation"] = explanation

        try:
            if adapter.is_nosql:
                columns, rows = await asyncio.to_thread(adapter.execute, query)
                total_rows = len(rows)
            elif is_system_query(query) or is_already_limited(query):
                columns, rows = await asyncio.to_thread(adapter.execute, query)
                total_rows = len(rows)
            else:
                paginated = paginate_sql(query, 1)
                columns, rows = await asyncio.to_thread(adapter.execute, paginated)
                total_rows = await asyncio.to_thread(safe_count, adapter, query)
        except Exception as e:
            if analysis_enabled:
                try:
                    from core.analyzer import ai_ask
                    db_name = session.get("active_db", "Unknown DB")
                    ai_result = await asyncio.to_thread(ai_ask, user_cmd, schema, db_name, dialect=dialect)
                    if "error" not in ai_result:
                        return {"task": "READ", "ai_response": ai_result.get("answer", ""),
                                "ai_suggestions": ai_result.get("suggested_queries", []),
                                "sql": query, "explanation": f"SQL failed ({str(e)}), AI answered directly."}
                except Exception:
                    pass
            return {"error": f"Execution failed: {str(e)}", "sql": query, "explanation": explanation, "task": "READ"}

        session["last_read_columns"] = columns

        return {
            "task": task, "sql": query, "explanation": explanation,
            "columns": columns,
            "results": rows_to_list(rows) if rows and not isinstance(rows[0], list) else rows if rows else [],
            "page": 1, "page_size": PAGE_SIZE, "total_rows": total_rows,
        }

    # WRITE / SCHEMA -> Redirect to review
    session["last_sql"] = query
    session["last_task"] = task
    session["last_explanation"] = explanation

    return {
        "needs_review": True, "sql": query, "explanation": explanation, "task": task,
    }


@router.get('/command/paginate')
async def api_paginate(request: Request, page: int = 1, adapter = Depends(get_active_adapter)):
    sql = request.scope.get("session", {}).get("last_read_sql")
    if not sql:
        raise HTTPException(status_code=400, detail="No active query to paginate")

    try:
        if adapter.is_nosql:
            columns, rows = await asyncio.to_thread(adapter.execute, sql)
            total_rows = len(rows)
        else:
            paginated = paginate_sql(sql, page)
            columns, rows = await asyncio.to_thread(adapter.execute, paginated)
            total_rows = await asyncio.to_thread(safe_count, adapter, sql)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Execution failed: {str(e)}")

    return {
        "task": "READ", "sql": sql, "explanation": request.scope.get("session", {}).get("last_explanation"),
        "columns": columns,
        "results": rows_to_list(rows) if rows and not isinstance(rows[0], list) else rows if rows else [],
        "page": page, "page_size": PAGE_SIZE, "total_rows": total_rows,
    }


# ---------------------------------------------------
# Write Execution
# ---------------------------------------------------
@router.post('/execute')
async def api_execute(
    request: Request,
    payload: ExecuteRequest,
    role: str = Depends(get_current_user_role),
    adapter = Depends(get_active_adapter)
):
    session = request.scope.get("session", {})
    query = payload.sql or session.get("last_sql")
    task = session.get("last_task")
    dialect = adapter.dialect

    if not query or not is_allowed(role, task) or not is_safe(query, dialect):
        raise HTTPException(status_code=403, detail="Permission denied or unsafe query.")

    if task in ("WRITE", "SCHEMA"):
        await asyncio.to_thread(take_snapshot, adapter, session.get("active_db", "Default SQLite"))

    try:
        await asyncio.to_thread(adapter.execute, query)
        session.pop("last_sql", None)
        session.pop("last_task", None)
        return {"success": True, "message": "Query executed successfully."}
    except Exception as e:
        return {"success": False, "error": f"Execution failed: {str(e)}"}


@router.post('/set-provider')
async def api_set_provider(request: Request, payload: SetProviderRequest):
    session = request.scope.get("session", {})
    session["llm_provider"] = payload.provider
    return {"success": True}


# ---------------------------------------------------
# Snapshots (Admin Only)
# ---------------------------------------------------
@router.post('/undo')
async def api_undo(request: Request, role: str = Depends(get_current_user_role), adapter = Depends(get_active_adapter)):
    if role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    if not adapter.supports_snapshot:
        return {"success": False, "error": "Undo not supported for this database"}
    try:
        active_db = request.scope.get("session", {}).get("active_db", "Default SQLite")
        await asyncio.to_thread(undo, 1, adapter, active_db)
        return {"success": True, "message": "Undo successful"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get('/snapshots')
async def api_list_snapshots():
    snaps = await asyncio.to_thread(list_snapshots)
    return {"snapshots": snaps}


@router.post('/snapshots')
async def api_create_snapshot(request: Request, role: str = Depends(get_current_user_role), adapter = Depends(get_active_adapter)):
    if role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    active_db = request.scope.get("session", {}).get("active_db", "Default SQLite")
    success = await asyncio.to_thread(take_snapshot, adapter, active_db)
    if success:
        return {"success": True, "message": f"Snapshot created for {active_db}"}
    return {"success": False, "error": "Failed to create snapshot"}


@router.post('/snapshots/restore')
async def api_restore_snapshot(payload: RestoreSnapshotRequest, role: str = Depends(get_current_user_role)):
    if role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        adapter = get_adapter_for_connection(payload.connection_name)
        success = await asyncio.to_thread(restore_snapshot, payload.snap_id, adapter)
        if success:
            return {"success": True}
        return {"success": False, "error": "Restore failed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.delete('/snapshots/{snap_id}')
async def api_delete_snapshot(snap_id: str, role: str = Depends(get_current_user_role)):
    if role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin only")
    success = await asyncio.to_thread(delete_snapshot, snap_id)
    if success:
        return {"success": True}
    return {"success": False, "error": "Failed to delete"}


# ---------------------------------------------------
# Admin Metrics & Dashboards
# ---------------------------------------------------
@router.get('/admin/metrics')
async def api_admin_metrics(role: str = Depends(get_current_user_role)):
    if role != "ADMIN":
        raise HTTPException(status_code=403, detail="Unauthorized")
    summary = await asyncio.to_thread(get_summary)
    llm_config = await asyncio.to_thread(llm_manager.load_config)
    ollama_models = await asyncio.to_thread(llm_manager.list_local_models)
    return {
        "summary": summary,
        "llm_config": llm_config,
        "ollama_models": ollama_models,
    }


@router.get('/dashboards')
async def api_list_dashboards():
    dashes = await asyncio.to_thread(list_dashboards)
    return {"dashboards": dashes}


@router.get('/dashboards/{dash_id}')
async def api_get_dashboard(dash_id: str):
    dash = await asyncio.to_thread(get_dashboard, dash_id)
    if not dash:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return dash


# ---------------------------------------------------
# Bootstrapper Schema Database Creator
# ---------------------------------------------------
@router.post('/create-database')
async def api_create_database(request: Request, payload: CreateDatabaseRequest):
    db_name = payload.db_name.strip()
    db_type = payload.db_type
    tables = payload.tables
    session = request.scope.get("session", {})

    if not db_name:
        return {"success": False, "error": "Database name required"}

    try:
        if db_type == "sqlite":
            import sqlite3
            path = f"db/{db_name}.db"
            os.makedirs("db/", exist_ok=True)
            
            def make_sqlite():
                conn = sqlite3.connect(path)
                try:
                    for tbl in tables:
                        if tbl.get("columns"):
                          col_defs = []
                          pks = []
                          for col in tbl["columns"]:
                              d = f'"{col["name"]}" {col.get("type", "TEXT")}'
                              if col.get("not_null"):
                                  d += " NOT NULL"
                              if col.get("pk"):
                                  pks.append(col["name"])
                              col_defs.append(d)
                          if pks:
                              pk_str = ", ".join(f'"{p}"' for p in pks)
                              col_defs.append(f"PRIMARY KEY ({pk_str})")
                          conn.execute(f'CREATE TABLE IF NOT EXISTS "{tbl["name"]}" ({", ".join(col_defs)})')
                    conn.commit()
                finally:
                    conn.close()
                add_connection(db_name, "sqlite", {"db_path": path})

            await asyncio.to_thread(make_sqlite)
        else:
            config = {k: request.query_params.get(k, "") for k in ["host", "port", "username", "password", "database", "service_name", "keyspace", "db_number"]}
            config["database"] = db_name
            await asyncio.to_thread(add_connection, db_name, db_type, config)

        session["active_db"] = db_name
        return {"success": True, "message": f"Database '{db_name}' created!"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------
# System Design Telemetry Endpoint
# ---------------------------------------------------
@router.get('/metrics')
async def api_system_metrics():
    """
    Exposes high-fidelity system design performance and health telemetry.
    """
    return {
        "rate_limiter": global_rate_limiter.get_telemetry(),
        "semantic_cache": global_semantic_cache.get_telemetry(),
        "circuit_breakers": cb_registry.list_breakers(),
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
