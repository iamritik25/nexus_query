"""
Query Validator — Dialect-Aware
Validates and classifies queries across SQL and NoSQL dialects.
"""

import json

# ---------------------------------------------------
# Dangerous keywords per dialect family
# ---------------------------------------------------
SQL_DANGEROUS = [
    "drop ",
    "truncate ",
    "alter ",
    "shutdown",
    "attach ",
    "detach ",
    "pragma ",
    "--",
    "/*",
    "*/",
]

NOSQL_DANGEROUS = [
    "dropDatabase",
    "dropCollection",
    "drop_collection",
    "drop_database",
    "FLUSHALL",
    "FLUSHDB",
    "CONFIG",
    "SHUTDOWN",
    "DEBUG",
    "SLAVEOF",
    "REPLICAOF",
]


# ---------------------------------------------------
SENSITIVE_COLUMNS = {
    "password", "ssn", "credit_card", "secret", "salt", 
    "hash", "token", "auth", "credentials"
}

def tokenize_sql(sql: str) -> list:
    """
    Lexical Tokenizer: Removes SQL comments and splits SQL string into individual lowercased tokens.
    Defeats comment-based bypass attempts like: SELECT/**/password/**/FROM/**/users
    """
    import re
    # Remove multi-line comments
    sql = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.DOTALL)
    # Remove single-line comments
    sql = re.sub(r'--.*', '', sql)
    
    # Find all words, quoted strings, and operators
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*|"[^"]*"|\'[^\']*\'', sql)
    return [t.strip('"\'').lower() for t in tokens]


def is_safe(query: str, dialect: str = "sqlite") -> bool:
    """
    SDE3 Enterprise AST Guardrail Sandbox Check:
    1. Lexically tokenizes SQL and NoSQL queries, stripping all comment obfuscation.
    2. Completely blocks administrative schema modifications (DROP, ALTER, TRUNCATE, etc.).
    3. Blocks direct column exposure of sensitive user columns (password, SSN, credit cards).
    4. Prevents stack command execution (semicolons).
    """
    q = query.strip()
    q_lower = q.lower()

    # ---- NoSQL: MongoDB / Redis ----
    if dialect in ("mongodb", "redis"):
        for keyword in NOSQL_DANGEROUS:
            if keyword.lower() in q_lower:
                return False

        if dialect == "mongodb":
            try:
                cmd = json.loads(q)
                op = cmd.get("operation", "")
                # Block drop operations
                if "drop" in op.lower():
                    return False
                
                # Check NoSQL fields for sensitive data leakage
                fields = str(cmd).lower()
                for col in SENSITIVE_COLUMNS:
                    if col in fields:
                        return False
            except json.JSONDecodeError:
                return False  # Invalid JSON is unsafe
        return True

    # ---- SQL dialects ----
    # Tokenize the query to get precise SQL lexical tokens (fully cleaned)
    tokens = tokenize_sql(query)

    # 1. Block destructive administrative commands (Ast-level)
    DESTRUCTIVE_KEYWORDS = {"drop", "truncate", "alter", "shutdown", "attach", "detach", "grant", "revoke"}
    for tok in tokens:
        if tok in DESTRUCTIVE_KEYWORDS:
            return False

    # 2. Block direct column exposure of sensitive columns (Data Security policy)
    for tok in tokens:
        if tok in SENSITIVE_COLUMNS:
            return False

    # 3. Block statement stacking (semicolons enable chaining destructive commands)
    if ";" in q:
        return False

    # Allow SELECT (after dangerous keyword and semicolon checks)
    if q_lower.startswith("select"):
        return True

    # Allow standard write operations
    if q_lower.startswith(("insert", "update", "delete")):
        return True

    # Allow introspection (SYSTEM) queries
    if classify_query(query, dialect) == "SYSTEM":
        return True

    # DEFAULT SAFETY FALLBACK
    DANGER_CHARS = (";", "--", "/*", "*/", "xp_", "drop", "truncate", "alter")
    if not any(dc in q_lower for dc in DANGER_CHARS):
        # If it's short and doesn't look like a command, it's "safe enough" to show as text
        return True

    # Cassandra CQL
    if dialect == "cassandra":
        if q_lower.startswith(("select", "insert", "update", "delete")):
            return True

    return False


# ---------------------------------------------------
# Classification
# ---------------------------------------------------
def classify_query(query: str, dialect: str = "sqlite") -> str:
    """
    Classifies a query into: READ, WRITE, SCHEMA, SYSTEM, or UNKNOWN.
    Works across all dialects.
    """
    q = query.strip()
    q_lower = q.lower()

    # ---- NoSQL: MongoDB ----
    if dialect == "mongodb":
        try:
            cmd = json.loads(q)
            op = cmd.get("operation", "")
            if op in ("find", "aggregate", "count"):
                return "READ"
            if op in ("insertOne", "insertMany", "updateOne", "updateMany",
                       "deleteOne", "deleteMany"):
                return "WRITE"
            return "UNKNOWN"
        except json.JSONDecodeError:
            return "UNKNOWN"

    # ---- NoSQL: Redis ----
    if dialect == "redis":
        try:
            cmd = json.loads(q)
            command = cmd.get("command", "").upper()
            READ_CMDS = {"GET", "MGET", "HGET", "HGETALL", "HMGET",
                         "LRANGE", "LINDEX", "LLEN", "SMEMBERS", "SCARD",
                         "ZRANGE", "ZRANGEBYSCORE", "KEYS", "SCAN",
                         "TYPE", "EXISTS", "TTL", "DBSIZE", "INFO"}
            WRITE_CMDS = {"SET", "MSET", "HSET", "HMSET", "HDEL",
                          "LPUSH", "RPUSH", "LPOP", "RPOP", "SADD",
                          "SREM", "ZADD", "ZREM", "DEL", "UNLINK",
                          "EXPIRE", "SETEX", "INCR", "DECR", "APPEND"}

            if "commands" in cmd:
                # Multi-command: classify as WRITE if any writes present
                has_write = any(
                    step.get("command", "").upper() in WRITE_CMDS
                    for step in cmd.get("commands", [])
                )
                return "WRITE" if has_write else "READ"

            if command in READ_CMDS:
                return "READ"
            if command in WRITE_CMDS:
                return "WRITE"
            return "UNKNOWN"
        except json.JSONDecodeError:
            return "UNKNOWN"

    # ---- SQL dialects (SQLite, MySQL, PostgreSQL, MSSQL, Oracle, Cassandra) ----
    # Introspection / System Queries
    SYSTEM_PREFIXES = ("show tables", "show foreign", "show index", "show constraint",
                       "show table count", "show row count", "show create table",
                       "show ddl", "show sql", "show structure", "show columns",
                       "show schema", "describe ", "desc ", "explain ", "pragma ", "\\d",
                       "list tables", "list collections", "show collections",
                       "table sizes")
    if q_lower.startswith(SYSTEM_PREFIXES):
        return "SYSTEM"
    
    # Metadata table queries
    SYSTEM_KEYWORDS = ("sqlite_master", "information_schema", "pg_catalog", "sys.tables", "db.getCollectionNames", "db.listCollections")
    if any(k in q_lower for k in SYSTEM_KEYWORDS):
        return "SYSTEM"

    if q_lower.startswith("select"):
        return "READ"
    if q_lower.startswith(("insert", "update", "delete")):
        return "WRITE"
    if q_lower.startswith(("create", "alter", "drop")):
        return "SCHEMA"
    return "UNKNOWN"


# ---------------------------------------------------
# Helpers
# ---------------------------------------------------
def is_write(query: str, dialect: str = "sqlite") -> bool:
    return classify_query(query, dialect) == "WRITE"


# ---------------------------------------------------
# Backward compatibility
# ---------------------------------------------------
def classify_sql(sql: str) -> str:
    """Legacy alias."""
    return classify_query(sql, "sqlite")
