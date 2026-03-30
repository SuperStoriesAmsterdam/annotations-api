"""
Annotation API — SuperStories
Shared microservice for website review feedback.
Flask + SQLite. One deploy, serves all projects.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
import sqlite3
import os

app = Flask(__name__)
CORS(app)

DB_PATH = os.environ.get("DB_PATH", "/data/annotations.db")
API_KEYS = os.environ.get("API_KEYS", "").split(",")  # comma-separated: "denimcity:key1,clientx:key2"
# Allowed origins: browser requests from these domains don't need an API key
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",")
# e.g. "https://denimcityhomepage.superstories.com,https://denimcity.superstories.com"


def get_db():
    """Get database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            page TEXT NOT NULL,
            block TEXT DEFAULT 'general',
            target TEXT DEFAULT 'claude',
            priority TEXT DEFAULT 'medium',
            text TEXT NOT NULL,
            name TEXT NOT NULL,
            x INTEGER DEFAULT 0,
            y INTEGER DEFAULT 0,
            status TEXT DEFAULT 'open',
            resolved_in TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_page ON annotations(project, page)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_target ON annotations(project, target, status)")
    conn.commit()
    conn.close()


def check_auth():
    """Check auth via API key OR allowed origin.
    Browser requests use Origin header (no key needed in HTML).
    CLI/script requests use X-Annotation-Key header.
    """
    # Check API key first (for CLI/Claude Code access)
    key = request.headers.get("X-Annotation-Key", "")
    for entry in API_KEYS:
        if ":" in entry:
            project, project_key = entry.strip().split(":", 1)
            if project_key == key:
                return project

    # Check Origin header (for browser requests from allowed sites)
    origin = request.headers.get("Origin", "")
    if origin and origin.strip() in [o.strip() for o in ALLOWED_ORIGINS if o.strip()]:
        return "browser"

    return None


def row_to_dict(row):
    """Convert sqlite3.Row to dict."""
    return dict(row)


# --- Routes ---

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/annotations", methods=["GET"])
def get_annotations():
    """Get annotations for a project/page."""
    project = request.args.get("project")
    page = request.args.get("page")
    target = request.args.get("target")
    status = request.args.get("status")

    if not project:
        return jsonify({"error": "project parameter required"}), 400

    auth_project = check_auth()
    if not auth_project:
        return jsonify({"error": "invalid API key"}), 401

    query = "SELECT * FROM annotations WHERE project = ?"
    params = [project]

    if page:
        query += " AND page = ?"
        params.append(page)
    if target:
        query += " AND target = ?"
        params.append(target)
    if status:
        query += " AND status = ?"
        params.append(status)

    query += " ORDER BY created_at DESC"

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()

    return jsonify([row_to_dict(r) for r in rows])


@app.route("/annotations", methods=["POST"])
def create_annotation():
    """Create a new annotation."""
    auth_project = check_auth()
    if not auth_project:
        return jsonify({"error": "invalid API key"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ["project", "page", "text", "name"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"'{field}' is required"}), 400

    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO annotations (project, page, block, target, priority, text, name, x, y, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
    """, (
        data["project"],
        data["page"],
        data.get("block", "general"),
        data.get("target", "claude"),
        data.get("priority", "medium"),
        data["text"],
        data["name"],
        data.get("x", 0),
        data.get("y", 0),
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    annotation_id = cursor.lastrowid

    row = conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
    conn.close()

    return jsonify(row_to_dict(row)), 201


@app.route("/annotations/<int:annotation_id>", methods=["PUT"])
def update_annotation(annotation_id):
    """Update an annotation (resolve, edit)."""
    auth_project = check_auth()
    if not auth_project:
        return jsonify({"error": "invalid API key"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    conn = get_db()
    existing = conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "not found"}), 404

    # Build update query from provided fields
    allowed = ["text", "block", "target", "priority", "status", "resolved_in"]
    updates = []
    params = []
    for field in allowed:
        if field in data:
            updates.append(f"{field} = ?")
            params.append(data[field])

    # Auto-set resolved_in when status changes to resolved
    if data.get("status") == "resolved" and "resolved_in" not in data:
        updates.append("resolved_in = ?")
        params.append(datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    if not updates:
        conn.close()
        return jsonify({"error": "no fields to update"}), 400

    params.append(annotation_id)
    conn.execute(f"UPDATE annotations SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()

    row = conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
    conn.close()

    return jsonify(row_to_dict(row))


@app.route("/annotations/<int:annotation_id>", methods=["DELETE"])
def delete_annotation(annotation_id):
    """Delete an annotation."""
    auth_project = check_auth()
    if not auth_project:
        return jsonify({"error": "invalid API key"}), 401

    conn = get_db()
    existing = conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "not found"}), 404

    conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
    conn.commit()
    conn.close()

    return jsonify({"deleted": annotation_id})


@app.route("/export", methods=["GET"])
def export_annotations():
    """Export annotations as JSON for claude or designer."""
    project = request.args.get("project")
    target = request.args.get("target")

    if not project or not target:
        return jsonify({"error": "project and target parameters required"}), 400

    auth_project = check_auth()
    if not auth_project:
        return jsonify({"error": "invalid API key"}), 401

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM annotations WHERE project = ? AND target = ? AND status = 'open' ORDER BY priority, created_at",
        (project, target)
    ).fetchall()
    conn.close()

    # Group by page and block
    grouped = {}
    for row in rows:
        r = row_to_dict(row)
        page = r["page"]
        if page not in grouped:
            grouped[page] = []
        grouped[page].append(r)

    return jsonify({
        "project": project,
        "target": target,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "pages": grouped
    })


# --- Startup ---

with app.app_context():
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
