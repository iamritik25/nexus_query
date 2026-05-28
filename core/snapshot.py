"""
Snapshot Engine
Cross-database snapshot management via adapter methods.
Maintains a registry of snapshots in db/snapshots.json.
"""

import os
import json
import uuid
from datetime import datetime

SNAP_DIR = "db/snapshots"
REGISTRY_FILE = "db/snapshots.json"
MAX_SNAPS_PER_DB = 2

os.makedirs(SNAP_DIR, exist_ok=True)


def _load_registry() -> list:
    if not os.path.exists(REGISTRY_FILE):
        return []
    try:
        with open(REGISTRY_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return []


def _save_registry(registry: list):
    with open(REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=2)


def get_snapshot(snap_id: str):
    registry = _load_registry()
    for snap in registry:
        if snap["id"] == snap_id:
            return snap
    return None


def list_snapshots(connection_name=None) -> list:
    """List snapshots, optionally filtered by connection name. Sorted newest first."""
    registry = _load_registry()
    if connection_name:
        registry = [s for s in registry if s.get("connection_name") == connection_name]
    return sorted(registry, key=lambda x: x["timestamp"], reverse=True)


def delete_snapshot(snap_id: str) -> bool:
    registry = _load_registry()
    snap = None
    for i, s in enumerate(registry):
        if s["id"] == snap_id:
            snap = registry.pop(i)
            break

    if snap:
        if os.path.exists(snap["file_path"]):
            try:
                os.remove(snap["file_path"])
            except OSError:
                pass
        _save_registry(registry)
        return True
    return False


def take_snapshot(adapter, connection_name: str):
    """
    Take a database snapshot using the adapter.
    Returns the snapshot metadata dict on success, or None on failure.
    """
    if not adapter or not adapter.supports_snapshot:
        return None

    registry = _load_registry()

    # Enforce limit per connection
    conn_snaps = [s for s in registry if s.get("connection_name") == connection_name]
    conn_snaps = sorted(conn_snaps, key=lambda x: x["timestamp"]) # oldest first

    if len(conn_snaps) >= MAX_SNAPS_PER_DB:
        # Delete oldest
        delete_snapshot(conn_snaps[0]["id"])
        registry = _load_registry() # Reload after delete

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_id = str(uuid.uuid4())[:8]
    ext = "db" if adapter.dialect == "sqlite" else "dump"
    if adapter.dialect == "mongodb":
        ext = "gz"

    filename = f"{connection_name}_{timestamp}_{snap_id}.{ext}"
    # Sanitizing filename just in case
    filename = filename.replace(" ", "_").replace("/", "-")
    filepath = os.path.join(SNAP_DIR, filename)

    success = adapter.take_snapshot(filepath)
    if success:
        snap = {
            "id": snap_id,
            "connection_name": connection_name,
            "db_type": adapter.dialect,
            "timestamp": datetime.now().isoformat(),
            "formatted_time": datetime.now().strftime("%b %d, %Y - %H:%M:%S"),
            "file_path": filepath
        }
        registry.append(snap)
        _save_registry(registry)
        return snap
    return None


def restore_snapshot(snap_id: str, adapter) -> bool:
    """
    Restore a specific snapshot.
    """
    snap = get_snapshot(snap_id)
    if not snap:
        raise ValueError("Snapshot not found.")

    if not adapter or not adapter.supports_snapshot:
        raise ValueError("Adapter does not support snapshots.")

    return adapter.restore_snapshot(snap["file_path"])


def undo(steps=1, adapter=None, connection_name="Default SQLite"):
    """
    Backwards compatibility: Restore the (steps)th most recent snapshot for the active DB.
    """
    snaps = list_snapshots(connection_name)
    if not snaps:
        raise Exception("No snapshots available to undo.")
        
    if steps > len(snaps):
        raise Exception("Undo state not available.")

    target_snap = snaps[steps - 1]
    success = restore_snapshot(target_snap["id"], adapter)
    if not success:
         raise Exception("Failed to restore snapshot.")


def has_snapshots(connection_name=None) -> bool:
    """Check if there are any snapshots available."""
    return len(list_snapshots(connection_name)) > 0


def self_heal_snapshots():
    """
    SDE3 Self-Healing Storage & Registry Optimization Routine:
    1. Prunes dead registry entries pointing to non-existent snapshot files.
    2. Deletes untracked/orphaned files in the snapshots folder to free up space.
    3. Enforces an upper-bound budget on total snapshot folder size (e.g. 20MB) to protect disk space.
    """
    registry = _load_registry()
    cleaned_registry = []
    registered_files = set()
    
    # 1. Prune dead entries whose files do not exist on disk
    for snap in registry:
        filepath = snap.get("file_path")
        if filepath and os.path.exists(filepath):
            cleaned_registry.append(snap)
            registered_files.add(os.path.abspath(filepath))
            
    # 2. Delete orphaned physical files not in the registry
    if os.path.exists(SNAP_DIR):
        for entry in os.scandir(SNAP_DIR):
            if entry.is_file():
                abs_path = os.path.abspath(entry.path)
                if abs_path not in registered_files:
                    try:
                        os.remove(entry.path)
                    except OSError:
                        pass

    # 3. Enforce a strict disk space budget of 20MB
    MAX_SNAP_BUDGET = 20 * 1024 * 1024  # 20 MB
    
    # Sort remaining active snapshots by timestamp (oldest first)
    cleaned_registry.sort(key=lambda x: x.get("timestamp", ""))
    
    while True:
        # Calculate current total snapshot folder size
        total_size = 0
        for snap in cleaned_registry:
            filepath = snap.get("file_path")
            if filepath and os.path.exists(filepath):
                total_size += os.path.getsize(filepath)
                
        if total_size <= MAX_SNAP_BUDGET or not cleaned_registry:
            break
            
        # Prune the oldest snapshot
        oldest = cleaned_registry.pop(0)
        if oldest and oldest.get("file_path") and os.path.exists(oldest["file_path"]):
            try:
                os.remove(oldest["file_path"])
            except OSError:
                pass
                
    _save_registry(cleaned_registry)

