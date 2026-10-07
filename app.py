import os
import shutil
import zipfile
import asyncio
import subprocess
import sys
import uuid
import threading
from datetime import datetime, timedelta
from fastapi import FastAPI, UploadFile, File, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter





app = FastAPI(title="Render & Firebase PaaS Cloud Hosting Engine")

UPLOAD_DIR = os.path.abspath("./user_apps")
os.makedirs(UPLOAD_DIR, exist_ok=True)

TELEGRAM_ADMIN = "JohnRipper1337"

FIREBASE_CONFIG = None

# Render/production: store the Firebase service-account JSON in
# FIREBASE_SERVICE_ACCOUNT_JSON, or use GOOGLE_APPLICATION_CREDENTIALS.
firebase_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
if firebase_json:
    try:
        import json
        FIREBASE_CONFIG = json.loads(firebase_json)
    except Exception as e:
        raise RuntimeError(f"Invalid FIREBASE_SERVICE_ACCOUNT_JSON: {e}")

if not firebase_admin._apps:
    if FIREBASE_CONFIG:
        cred = credentials.Certificate(FIREBASE_CONFIG)
        firebase_admin.initialize_app(cred)
    else:
        # GOOGLE_APPLICATION_CREDENTIALS is supported automatically by Firebase/Google SDK.
        firebase_admin.initialize_app()

db = firestore.client()

# app_id -> subprocess.Popen
RUNNING_PROCESSES = {}
# app_id -> list[str] containing the most recent process output.
PROCESS_LOGS = {}
PROCESS_LOG_LOCK = threading.RLock()

def init_default_admin():
    try:
        users_ref = db.collection('users').document('admin')
        if not users_ref.get().exists:
            one_year = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
            users_ref.set({
                "username": "admin",
                "password": os.environ.get("ADMIN_PASSWORD", "CHANGE_ME"),
                "role": "admin",
                "max_slots": 999,
                "slot_expiry": one_year,
                "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
    except Exception as e:
        print(f"[ADMIN INIT ERROR] {e}")

init_default_admin()

def get_client_ip(request: Request):
    x_forwarded = request.headers.get("X-Forwarded-For")
    if x_forwarded:
        return x_forwarded.split(",")[0]
    return request.client.host

def find_and_setup_entry_script(project_dir):
    items = os.listdir(project_dir)
    if len(items) == 1 and os.path.isdir(os.path.join(project_dir, items[0])):
        nested_folder = os.path.join(project_dir, items[0])
        for sub_item in os.listdir(nested_folder):
            shutil.move(os.path.join(nested_folder, sub_item), project_dir)
        os.rmdir(nested_folder)

    if os.path.exists(os.path.join(project_dir, "main.py")):
        return "main.py"
    
    py_files = [f for f in os.listdir(project_dir) if f.endswith(".py")]
    if py_files:
        if "bot.py" in py_files:
            return "bot.py"
        if "app.py" in py_files:
            return "app.py"
        return py_files[0]
        
    return None

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Render & Firebase PaaS Engine</title>
    <style>
        :root {
            --bg-color: #0f172a; --card-bg: #1e293b; --accent-color: #6366f1;
            --danger-color: #ef4444; --text-color: #f8fafc; --text-muted: #94a3b8;
            --border-color: rgba(255, 255, 255, 0.1);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', Tahoma, sans-serif; }
        body { background-color: var(--bg-color); color: var(--text-color); min-height: 100vh; }
        header { background: rgba(30, 41, 59, 0.9); padding: 1rem 2rem; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); }
        .logo { font-size: 1.5rem; font-weight: bold; color: #a855f7; }
        .container { max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.5rem; margin-top: 1.5rem; }
        .card { background: var(--card-bg); border-radius: 12px; padding: 1.5rem; border: 1px solid var(--border-color); }
        .form-group { margin-bottom: 1rem; }
        .form-group label { display: block; margin-bottom: 0.4rem; color: var(--text-muted); }
        .form-group input, .form-group select { width: 100%; padding: 0.8rem; background: var(--bg-color); border: 1px solid var(--border-color); color: var(--text-color); border-radius: 8px; outline: none; }
        .btn { background: var(--accent-color); color: white; border: none; padding: 0.7rem 1.2rem; border-radius: 8px; cursor: pointer; font-weight: 600; width: 100%; text-decoration: none; display: inline-block; text-align: center; }
        .btn-sm { width: auto; padding: 0.35rem 0.7rem; font-size: 0.8rem; margin-right: 4px; }
        .btn-google { background: #ea4335; margin-top: 0.5rem; display: flex; align-items: center; justify-content: center; gap: 8px; }
        .btn-tg { background: #0088cc; margin-top: 0.5rem; }
        .btn-success { background: #22c55e; }
        .btn-warning { background: #f59e0b; color: #fff; }
        .btn-danger { background: var(--danger-color); }
        .terminal-box { background: #020617; border: 1px solid var(--border-color); border-radius: 8px; padding: 1rem; font-family: monospace; font-size: 0.85rem; color: #38bdf8; height: 260px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
        .badge { display: inline-block; padding: 0.25rem 0.6rem; border-radius: 6px; font-size: 0.75rem; font-weight: bold; }
        .badge-running { background: rgba(34, 197, 94, 0.2); color: #4ade80; }
        .badge-stopped { background: rgba(239, 68, 68, 0.2); color: #f87171; }
        .badge-admin { background: rgba(168, 85, 247, 0.2); color: #c084fc; }

        .chat-widget { position: fixed; bottom: 20px; right: 20px; z-index: 1000; }
        .chat-btn { background: var(--accent-color); color: white; border: none; border-radius: 50px; padding: 12px 20px; font-weight: bold; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
        .chat-box { display: none; position: fixed; bottom: 80px; right: 20px; width: 330px; height: 420px; background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); flex-direction: column; overflow: hidden; z-index: 1000; }
        .chat-header { background: #0f172a; padding: 10px 15px; font-weight: bold; display: flex; justify-content: space-between; border-bottom: 1px solid var(--border-color); }
        .chat-messages { flex: 1; padding: 10px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; font-size: 0.85rem; }
        .msg { padding: 8px 12px; border-radius: 8px; max-width: 80%; word-break: break-all; }
        .msg-me { background: var(--accent-color); align-self: flex-end; color: white; }
        .msg-other { background: #334155; align-self: flex-start; color: white; }
        .chat-input { display: flex; padding: 8px; border-top: 1px solid var(--border-color); background: #0f172a; }
        .chat-input input { flex: 1; background: transparent; border: none; color: white; padding: 5px; outline: none; }
    </style>
</head>
<body>
    <header>
        <div class="logo">Render PaaS Engine</div>
        <div>
            {% if current_user %}
                <span>User: <strong>{{ current_user }}</strong> {% if is_admin %}<span class="badge badge-admin">ADMIN</span>{% endif %}</span> | 
                <a href="https://t.me/{{ tg_admin }}" target="_blank" style="color: #38bdf8; text-decoration: none;">Buy Extra Slots</a> | 
                <a href="/logout" style="color: var(--danger-color); text-decoration: none;">Logout</a>
            {% endif %}
        </div>
    </header>

    <div class="container">
        {% if not current_user %}
            <div style="max-width: 420px; margin: 3rem auto;" class="card">
                <h2>Login / Register</h2>
                {% if error %}<p style="color: var(--danger-color); margin-top: 0.8rem; font-size: 0.9rem;">{{ error }}</p>{% endif %}
                <form action="/auth" method="post" style="margin-top: 1.2rem;">
                    <div class="form-group">
                        <label>Username</label>
                        <input type="text" name="username" required>
                    </div>
                    <div class="form-group">
                        <label>Password</label>
                        <input type="password" name="password" required>
                    </div>
                    <button type="submit" name="action" value="login" class="btn" style="margin-bottom: 0.5rem;">Login</button>
                    <button type="submit" name="action" value="register" class="btn" style="background: #334155;">Register Account</button>
                </form>

                <div style="text-align: center; margin: 1rem 0; color: var(--text-muted); position: relative;">
                    <span style="background: var(--card-bg); padding: 0 10px; position: relative; z-index: 1;">OR</span>
                    <hr style="position: absolute; top: 50%; width: 100%; border: none; border-top: 1px solid var(--border-color);">
                </div>

                <button type="button" onclick="loginWithGoogle()" class="btn btn-google">
                    🌐 Sign in with Google
                </button>
            </div>
        {% else %}
            
            {% if is_admin %}
            <h2 style="color: #c084fc; margin-bottom: 1rem;">Admin Dashboard</h2>
            
            <div class="grid" style="margin-bottom: 1.5rem;">
                <div class="card" style="text-align: center;">
                    <h3>Total Users</h3>
                    <p style="font-size: 2rem; font-weight: bold; color: #a855f7;">{{ total_users }}</p>
                </div>
                <div class="card" style="text-align: center;">
                    <h3>Online Users</h3>
                    <p style="font-size: 2rem; font-weight: bold; color: #22c55e;">{{ online_users }}</p>
                </div>
                <div class="card" style="text-align: center;">
                    <h3>Offline Users</h3>
                    <p style="font-size: 2rem; font-weight: bold; color: #94a3b8;">{{ offline_users }}</p>
                </div>
            </div>

            <div class="grid" style="margin-bottom: 2rem;">
                <div class="card">
                    <h3>Change Password</h3>
                    <form action="/admin/change-my-pass" method="post" style="margin-top: 1rem;">
                        <div class="form-group">
                            <input type="password" name="new_password" required placeholder="New Password">
                        </div>
                        <button type="submit" class="btn">Update Password</button>
                    </form>
                </div>

                <div class="card">
                    <h3>Reset User Pass</h3>
                    <form action="/admin/change-user-pass" method="post" style="margin-top: 1rem;">
                        <div class="form-group">
                            <input type="text" name="target_username" required placeholder="Target Username">
                        </div>
                        <div class="form-group">
                            <input type="password" name="new_password" required placeholder="New Password">
                        </div>
                        <button type="submit" class="btn btn-success">Reset Password</button>
                    </form>
                </div>

                <div class="card">
                    <h3>Manage User Slots</h3>
                    <form action="/admin/manage-slots" method="post" style="margin-top: 1rem;">
                        <div class="form-group">
                            <input type="text" name="target_username" required placeholder="Target Username">
                        </div>
                        <div class="form-group">
                            <input type="number" name="slot_count" min="0" required placeholder="Slots Count">
                        </div>
                        <div class="form-group">
                            <label>Validity Duration</label>
                            <select name="duration_months" required>
                                <option value="1">1 Month</option>
                                <option value="2">2 Months</option>
                                <option value="3">3 Months</option>
                                <option value="6">6 Months</option>
                                <option value="9">9 Months</option>
                                <option value="10">10 Months</option>
                                <option value="12">12 Months (1 Year)</option>
                            </select>
                        </div>
                        <div style="display: flex; gap: 10px;">
                            <button type="submit" name="action" value="set" class="btn btn-success">Set Slots</button>
                            <button type="submit" name="action" value="revoke" class="btn btn-danger">Revoke</button>
                        </div>
                    </form>
                </div>
            </div>

            <div class="card" style="margin-bottom: 2rem;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <h3>Registered Users Directory</h3>
                    <form action="/" method="get" style="display: flex; gap: 8px;">
                        <input type="text" name="search_user" value="{{ search_query }}" placeholder="Search username..." style="padding: 0.5rem; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-color); color: white;">
                        <button type="submit" class="btn" style="width: auto; padding: 0.5rem 1rem;">Search</button>
                    </form>
                </div>

                <div style="overflow-x: auto; margin-top: 1rem;">
                    <table style="width: 100%; border-collapse: collapse; text-align: left;">
                        <thead>
                            <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-muted);">
                                <th style="padding: 0.8rem;">Username</th>
                                <th>Role</th>
                                <th>Max Slots</th>
                                <th>Slot Expiry</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for u in all_registered_users %}
                            <tr style="border-bottom: 1px solid var(--border-color);">
                                <td style="padding: 0.8rem;"><strong>{{ u.username }}</strong></td>
                                <td>{{ u.role }}</td>
                                <td>{{ u.max_slots }}</td>
                                <td style="font-size: 0.85rem; color: #38bdf8;">{{ u.slot_expiry }}</td>
                                <td>
                                    {% if u.is_online %}
                                        <span class="badge" style="background: rgba(34, 197, 94, 0.2); color: #22c55e;">Online</span>
                                    {% else %}
                                        <span class="badge" style="background: rgba(148, 163, 184, 0.2); color: #94a3b8;">Offline</span>
                                    {% endif %}
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" style="padding: 1rem; color: var(--text-muted); text-align: center;">No users found.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
            {% endif %}

            <div class="card" style="margin-bottom: 1.5rem; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h3>Slot Status: {{ active_apps_count }} / {{ max_slots }} Used</h3>
                    <p style="color: var(--text-muted); font-size: 0.85rem;">Slot Expiry: <strong>{{ slot_expiry }}</strong></p>
                </div>
                <a href="https://t.me/{{ tg_admin }}" target="_blank" class="btn btn-tg" style="width: auto;">Buy Slots (@{{ tg_admin }})</a>
            </div>

            <div class="grid">
                <div class="card">
                    <h3>Deploy Bot / App</h3>
                    <p style="color: var(--text-muted); font-size: 0.8rem; margin-bottom: 1rem;">Upload Python file (.py) or ZIP archive (.zip)</p>
                    <form action="/deploy" method="post" enctype="multipart/form-data">
                        <div class="form-group">
                            <label>App / Bot Name</label>
                            <input type="text" name="app_name" placeholder="e.g. my-bot" required>
                        </div>
                        <div class="form-group">
                            <label>Upload Script or ZIP</label>
                            <input type="file" name="zip_file" required>
                        </div>
                        <button type="submit" class="btn">Deploy Project</button>
                    </form>
                </div>

                <div class="card">
                    <h3>Terminal Console Log</h3>
                    <div class="terminal-box" id="terminalLog">Select an active bot and click 'Console' to view logs...</div>
                </div>
            </div>

            <div class="card" style="margin-top: 2rem;">
                <h3>Active Applications</h3>
                <div style="overflow-x: auto; margin-top: 1rem;">
                    <table style="width: 100%; border-collapse: collapse; text-align: left;">
                        <thead>
                            <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-muted);">
                                <th style="padding: 0.8rem;">App Name</th>
                                <th>ID</th>
                                <th>Owner</th>
                                <th>Status</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for app in user_apps %}
                            <tr style="border-bottom: 1px solid var(--border-color);">
                                <td style="padding: 0.8rem;"><strong>{{ app.app_name }}</strong></td>
                                <td style="font-family: monospace; color: #38bdf8;">{{ app.id }}</td>
                                <td>{{ app.username }}</td>
                                <td>
                                    <span class="badge {% if app.status == 'running' %}badge-running{% else %}badge-stopped{% endif %}">
                                        {{ app.status }}
                                    </span>
                                </td>
                                <td>
                                    <form action="/start-app/{{ app.id }}" method="post" style="display: inline;">
                                        <button class="btn btn-sm btn-success">Start</button>
                                    </form>
                                    <form action="/reload-app/{{ app.id }}" method="post" style="display: inline;">
                                        <button class="btn btn-sm btn-warning">Reload</button>
                                    </form>
                                    <button class="btn btn-sm" onclick="connectConsole('{{ app.id }}')">Console</button>
                                    <form action="/delete-app/{{ app.id }}" method="post" style="display: inline;">
                                        <button class="btn btn-sm btn-danger" onclick="return confirm('Delete this bot?')">Delete</button>
                                    </form>
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" style="padding: 1rem; color: var(--text-muted); text-align: center;">No active applications found.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="chat-widget">
                <button class="chat-btn" onclick="toggleChat()">💬 Support Chat</button>
                <div class="chat-box" id="chatBox">
                    <div class="chat-header">
                        <span>Live Support</span>
                        <span style="cursor: pointer;" onclick="toggleChat()">✖</span>
                    </div>

                    {% if is_admin %}
                    <div style="padding: 5px; background: #0f172a; border-bottom: 1px solid var(--border-color);">
                        <select id="chatTargetUser" style="padding: 5px; font-size: 0.8rem;">
                            <option value="">Select User to Chat</option>
                            {% for u in all_users_list %}
                                <option value="{{ u.username }}">{{ u.username }} ({% if u.is_online %}Online{% else %}Offline{% endif %})</option>
                            {% endfor %}
                        </select>
                    </div>
                    {% endif %}

                    <div class="chat-messages" id="chatMessages">
                        <div class="msg msg-other">Welcome to Live Support!</div>
                    </div>

                    <div class="chat-input">
                        <input type="text" id="chatMsgInput" placeholder="Type a message..." onkeypress="if(event.key==='Enter') sendChatMessage()">
                        <button onclick="sendChatMessage()" style="background: var(--accent-color); color: white; border: none; padding: 5px 10px; border-radius: 4px; cursor: pointer;">Send</button>
                    </div>
                </div>
            </div>
        {% endif %}
    </div>

    <script src="https://www.gstatic.com/firebasejs/9.22.0/firebase-app-compat.js"></script>
    <script src="https://www.gstatic.com/firebasejs/9.22.0/firebase-auth-compat.js"></script>

    <script>
        const firebaseConfig = {
            projectId: "hosting-bf464",
            authDomain: "hosting-bf464.firebaseapp.com"
        };
        firebase.initializeApp(firebaseConfig);

        async function loginWithGoogle() {
            const provider = new firebase.auth.GoogleAuthProvider();
            try {
                const result = await firebase.auth().signInWithPopup(provider);
                const user = result.user;
                const username = user.email.split('@')[0].replace(/[^a-zA-Z0-9]/g, "").toLowerCase();
                
                const formData = new FormData();
                formData.append("username", username);
                formData.append("password", user.uid);
                formData.append("action", "google_login");

                const res = await fetch('/auth', { method: 'POST', body: formData });
                if(res.redirected) window.location.href = res.url;
                else window.location.reload();
            } catch (err) {
                alert("Google Sign-In Error: " + err.message);
            }
        }

        let activeLogInterval = null;
        function connectConsole(appId) {
            const terminal = document.getElementById('terminalLog');
            terminal.innerText = "Connecting to App ID [" + appId + "]...\n";
            if(activeLogInterval) clearInterval(activeLogInterval);
            
            async function fetchLogs() {
                try {
                    const res = await fetch(`/get-app-logs/${appId}?t=` + new Date().getTime(), { credentials: "same-origin" });
                    const data = await res.json();
                    if (data.logs && data.logs.trim() !== "") {
                        terminal.innerText = data.logs;
                    } else {
                        terminal.innerText = (data.logs || "Waiting for process logs...\n");
                    }
                    terminal.scrollTop = terminal.scrollHeight;
                } catch(e) {}
            }
            fetchLogs();
            activeLogInterval = setInterval(fetchLogs, 500);
        }

        function toggleChat() {
            const cb = document.getElementById('chatBox');
            cb.style.display = (cb.style.display === 'flex') ? 'none' : 'flex';
            if(cb.style.display === 'flex') { fetchMessages(); }
        }

        async function fetchMessages() {
            const isAdmin = {{ 'true' if is_admin else 'false' }};
            let targetUser = 'admin';

            if(isAdmin) {
                targetUser = document.getElementById('chatTargetUser').value;
                if(!targetUser) return;
            }

            const res = await fetch(`/get-messages?target=${targetUser}`);
            const data = await res.json();
            const msgBox = document.getElementById('chatMessages');
            msgBox.innerHTML = '';

            data.messages.forEach(m => {
                const isMe = (m.sender === '{{ current_user }}');
                const div = document.createElement('div');
                div.className = 'msg ' + (isMe ? 'msg-me' : 'msg-other');
                div.innerText = (isAdmin ? `[${m.sender}]: ` : '') + m.message;
                msgBox.appendChild(div);
            });
            msgBox.scrollTop = msgBox.scrollHeight;
        }

        async function sendChatMessage() {
            const input = document.getElementById('chatMsgInput');
            const msg = input.value.trim();
            if(!msg) return;

            const isAdmin = {{ 'true' if is_admin else 'false' }};
            let targetUser = 'admin';

            if(isAdmin) {
                targetUser = document.getElementById('chatTargetUser').value;
                if(!targetUser) { alert('Select a user first!'); return; }
            }

            await fetch('/send-message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: `receiver=${targetUser}&message=${encodeURIComponent(msg)}`
            });

            input.value = '';
            fetchMessages();
        }

        setInterval(() => {
            const cb = document.getElementById('chatBox');
            if(cb.style.display === 'flex') { fetchMessages(); }
        }, 3000);
    </script>
</body>
</html>
"""

ACTIVE_SESSIONS = {}

def get_current_session_id(request: Request, fallback: str = ""):
    return request.cookies.get("session_id") or fallback or ""

def get_current_user(request: Request, fallback: str = ""):
    return ACTIVE_SESSIONS.get(get_current_session_id(request, fallback))


@app.get("/", response_class=HTMLResponse)
def index_page(request: Request, session_id: str = None, search_user: str = None):
    from jinja2 import Template
    session_id = get_current_session_id(request, session_id)
    current_user = ACTIVE_SESSIONS.get(session_id)
    user_apps, max_slots, slot_expiry, is_admin = [], 1, "N/A", False
    total_users, online_users, offline_users = 0, 0, 0
    all_users_list = []
    all_registered_users = []

    if current_user:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            db.collection('users').document(current_user).update({"last_seen": now_str})
        except Exception:
            pass

        u_doc = db.collection('users').document(current_user).get()
        if u_doc.exists:
            u_data = u_doc.to_dict()
            max_slots = u_data.get("max_slots", 1)
            slot_expiry = u_data.get("slot_expiry", "N/A")
            is_admin = (u_data.get("role") == "admin")

        if is_admin:
            apps_docs = db.collection('apps').stream()
            user_apps = [{"id": d.id, **d.to_dict()} for d in apps_docs]

            users_docs = db.collection('users').stream()
            five_mins_ago = datetime.now() - timedelta(minutes=5)

            for ud in users_docs:
                u = ud.to_dict()
                username = u.get("username")
                last_seen_str = u.get("last_seen")
                role = u.get("role", "user")

                is_online = False
                if last_seen_str:
                    try:
                        last_seen_dt = datetime.strptime(last_seen_str, "%Y-%m-%d %H:%M:%S")
                        if last_seen_dt > five_mins_ago:
                            is_online = True
                    except Exception:
                        pass

                if role != "admin":
                    total_users += 1
                    if is_online:
                        online_users += 1
                    else:
                        offline_users += 1
                    all_users_list.append({"username": username, "is_online": is_online})

                user_item = {
                    "username": username,
                    "role": role,
                    "max_slots": u.get("max_slots", 1),
                    "slot_expiry": u.get("slot_expiry", "N/A"),
                    "is_online": is_online
                }

                if search_user:
                    if search_user.lower() in username.lower():
                        all_registered_users.append(user_item)
                else:
                    all_registered_users.append(user_item)

        else:
            apps_docs = db.collection('apps').where(filter=FieldFilter('username', '==', current_user)).stream()
            user_apps = [{"id": d.id, **d.to_dict()} for d in apps_docs]

    tmpl = Template(HTML_TEMPLATE)
    return tmpl.render(
        current_user=current_user,
        is_admin=is_admin,
        user_apps=user_apps,
        active_apps_count=len(user_apps),
        max_slots=max_slots,
        slot_expiry=slot_expiry,
        tg_admin=TELEGRAM_ADMIN,
        total_users=total_users,
        online_users=online_users,
        offline_users=offline_users,
        all_users_list=all_users_list,
        all_registered_users=all_registered_users,
        search_query=search_user or "",
        request_session_id=session_id or "",
        error=None
    )

@app.post("/auth")
def authenticate(request: Request, username: str = Form(...), password: str = Form(...), action: str = Form(...)):
    client_ip = get_client_ip(request)

    if action == "google_login":
        user_ref = db.collection('users').document(username)
        u_doc = user_ref.get()

        if not u_doc.exists:
            expiry_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            user_ref.set({
                "username": username,
                "password": password,
                "role": "user",
                "max_slots": 1,
                "slot_expiry": expiry_date,
                "last_seen": now_str
            })

        session_id = f"sess_{username}_{uuid.uuid4().hex}"
        ACTIVE_SESSIONS[session_id] = username
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie("session_id", session_id, httponly=True, samesite="lax", secure=False, max_age=86400)
        return response

    if action == "register":
        ip_docs = db.collection('user_ips').where(filter=FieldFilter('ip_address', '==', client_ip)).stream()
        ip_list = [d.to_dict() for d in ip_docs]

        for ip_item in ip_list:
            blocked_until_str = ip_item.get("blocked_until")
            if blocked_until_str:
                try:
                    blocked_until = datetime.strptime(blocked_until_str, "%Y-%m-%d %H:%M:%S")
                    if datetime.now() < blocked_until:
                        from jinja2 import Template
                        return HTMLResponse(content=Template(HTML_TEMPLATE).render(
                            current_user=None, tg_admin=TELEGRAM_ADMIN,
                            error="IP Blocked! Multiple account creations detected. Try again after 24 Hours."
                        ))
                except Exception:
                    pass

        if len(ip_list) >= 1:
            block_time = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            db.collection('user_ips').add({
                "ip_address": client_ip,
                "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "blocked_until": block_time
            })
            from jinja2 import Template
            return HTMLResponse(content=Template(HTML_TEMPLATE).render(
                current_user=None, tg_admin=TELEGRAM_ADMIN,
                error="Account limit reached for this IP! Blocked for 24 hours."
            ))

        user_ref = db.collection('users').document(username)
        if user_ref.get().exists:
            from jinja2 import Template
            return HTMLResponse(content=Template(HTML_TEMPLATE).render(current_user=None, tg_admin=TELEGRAM_ADMIN, error="Username already exists!"))

        expiry_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        user_ref.set({
            "username": username,
            "password": password,
            "role": "user",
            "max_slots": 1,
            "slot_expiry": expiry_date,
            "last_seen": now_str
        })

        db.collection('user_ips').add({
            "ip_address": client_ip,
            "registered_at": now_str,
            "blocked_until": None
        })

    user_ref = db.collection('users').document(username)
    u_doc = user_ref.get()
    if u_doc.exists and u_doc.to_dict().get("password") == password:
        session_id = f"sess_{username}_{uuid.uuid4().hex}"
        ACTIVE_SESSIONS[session_id] = username
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie("session_id", session_id, httponly=True, samesite="lax", secure=False, max_age=86400)
        return response

    from jinja2 import Template
    return HTMLResponse(content=Template(HTML_TEMPLATE).render(current_user=None, tg_admin=TELEGRAM_ADMIN, error="Invalid Credentials!"))

@app.get("/get-messages")
def get_messages(request: Request, target: str):
    session_id = get_current_session_id(request)
    current_user = ACTIVE_SESSIONS.get(session_id)
    if not current_user:
        return {"messages": []}

    msgs_ref = db.collection('messages').stream()
    all_msgs = [m.to_dict() for m in msgs_ref]

    filtered_msgs = []
    for m in all_msgs:
        s, r = m.get("sender"), m.get("receiver")
        if (s == current_user and r == target) or (s == target and r == current_user):
            filtered_msgs.append(m)

    filtered_msgs.sort(key=lambda x: x.get("timestamp", ""))
    return {"messages": filtered_msgs}

@app.post("/send-message")
def send_message(request: Request, receiver: str = Form(...), message: str = Form(...)):
    session_id = get_current_session_id(request)
    current_user = ACTIVE_SESSIONS.get(session_id)
    if not current_user or not message.strip():
        return {"status": "error"}

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.collection('messages').add({
        "sender": current_user,
        "receiver": receiver,
        "message": message,
        "timestamp": now_str
    })
    return {"status": "ok"}

@app.post("/admin/change-my-pass")
def admin_change_my_pass(request: Request, new_password: str = Form(...)):
    session_id = get_current_session_id(request)
    current_user = ACTIVE_SESSIONS.get(session_id)
    if current_user:
        db.collection('users').document(current_user).update({"password": new_password})
    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/change-user-pass")
def admin_change_user_pass(request: Request, target_username: str = Form(...), new_password: str = Form(...)):
    session_id = get_current_session_id(request)
    current_user = ACTIVE_SESSIONS.get(session_id)
    if current_user:
        u_doc = db.collection('users').document(current_user).get()
        if u_doc.exists and u_doc.to_dict().get("role") == "admin":
            db.collection('users').document(target_username).update({"password": new_password})
    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/manage-slots")
def admin_manage_slots(request: Request, target_username: str = Form(...), slot_count: int = Form(...), duration_months: int = Form(1), action: str = Form(...)):
    session_id = get_current_session_id(request)
    current_user = ACTIVE_SESSIONS.get(session_id)
    if current_user:
        u_doc = db.collection('users').document(current_user).get()
        if u_doc.exists and u_doc.to_dict().get("role") == "admin":
            if action == "revoke":
                new_slots = 0
                new_expiry = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            else:
                new_slots = slot_count
                days_to_add = duration_months * 30
                new_expiry = (datetime.now() + timedelta(days=days_to_add)).strftime("%Y-%m-%d %H:%M:%S")

            db.collection('users').document(target_username).update({
                "max_slots": new_slots,
                "slot_expiry": new_expiry
            })
    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/logout")
def logout(request: Request):
    session_id = get_current_session_id(request)
    if session_id:
        ACTIVE_SESSIONS.pop(session_id, None)
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("session_id")
    return response

def _append_process_log(app_id, message):
    if message is None:
        return
    message = str(message).rstrip("\\r\\n")
    if not message:
        return
    with PROCESS_LOG_LOCK:
        lines = PROCESS_LOGS.setdefault(app_id, [])
        lines.extend(message.splitlines() or [message])
        if len(lines) > 500:
            del lines[:-500]

    try:
        app_doc = db.collection("apps").document(app_id).get()
        if app_doc.exists:
            data = app_doc.to_dict()
            log_path = os.path.join(
                UPLOAD_DIR, data.get("username", ""), data.get("app_name", ""), "output.log"
            )
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8", buffering=1) as f:
                f.write(message + "\\n")
    except Exception:
        pass


def _stream_process_output(app_id, stream):
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            _append_process_log(app_id, line)
    except Exception as e:
        _append_process_log(app_id, f"[LOG READER ERROR] {e}")
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _watch_process(app_id, proc):
    rc = proc.wait()
    _append_process_log(app_id, f"[PROCESS EXIT] exited with code {rc}")
    with PROCESS_LOG_LOCK:
        if RUNNING_PROCESSES.get(app_id) is proc:
            RUNNING_PROCESSES.pop(app_id, None)
    try:
        db.collection("apps").document(app_id).update({"status": "stopped", "exit_code": rc})
    except Exception:
        pass


def run_app_process(app_id, project_dir, entry_script):
    os.makedirs(project_dir, exist_ok=True)
    req_file = os.path.join(project_dir, "requirements.txt")

    if os.path.exists(req_file):
        try:
            _append_process_log(app_id, "[SYSTEM] Installing requirements.txt ...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req_file],
                check=False, cwd=project_dir
            )
        except Exception as e:
            _append_process_log(app_id, f"[PIP ERROR] {e}")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    with PROCESS_LOG_LOCK:
        PROCESS_LOGS[app_id] = []

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(project_dir, entry_script)],
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        RUNNING_PROCESSES[app_id] = proc
        _append_process_log(app_id, f"[PROCESS STARTED] PID={proc.pid}")

        threading.Thread(
            target=_stream_process_output,
            args=(app_id, proc.stdout),
            daemon=True
        ).start()
        threading.Thread(
            target=_watch_process,
            args=(app_id, proc),
            daemon=True
        ).start()
        return "running"
    except Exception as e:
        _append_process_log(app_id, f"[PROCESS START ERROR] {e}")
        return "error"

@app.post("/deploy")
async def deploy_app(app_name: str = Form(...), zip_file: UploadFile = File(...)):
    if not ACTIVE_SESSIONS:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    session_id = get_current_session_id(request)
    username = ACTIVE_SESSIONS.get(session_id)

    u_doc = db.collection('users').document(username).get()
    if not u_doc.exists:
        return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

    u_data = u_doc.to_dict()
    max_slots = u_data.get("max_slots", 1)
    slot_expiry_str = u_data.get("slot_expiry", "")

    try:
        slot_expiry = datetime.strptime(slot_expiry_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        slot_expiry = datetime.now() + timedelta(days=30)

    user_apps_docs = list(db.collection('apps').where(filter=FieldFilter('username', '==', username)).stream())
    if datetime.now() > slot_expiry or len(user_apps_docs) >= max_slots:
        return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

    safe_app_name = "".join(e for e in app_name if e.isalnum()).lower()
    app_id = f"{username}_{safe_app_name}"
    project_dir = os.path.join(UPLOAD_DIR, username, safe_app_name)

    if os.path.exists(project_dir):
        shutil.rmtree(project_dir, ignore_errors=True)
    os.makedirs(project_dir, exist_ok=True)

    file_filename = zip_file.filename
    uploaded_file_path = os.path.join(project_dir, file_filename)

    with open(uploaded_file_path, "wb") as buffer:
        shutil.copyfileobj(zip_file.file, buffer)

    if file_filename.endswith(".zip"):
        try:
            with zipfile.ZipFile(uploaded_file_path, 'r') as zip_ref:
                base = os.path.realpath(project_dir)
                for member in zip_ref.infolist():
                    target = os.path.realpath(os.path.join(project_dir, member.filename))
                    if not target.startswith(base + os.sep) and target != base:
                        raise ValueError(f"Unsafe ZIP path: {member.filename}")
                zip_ref.extractall(project_dir)
            os.remove(uploaded_file_path)
        except Exception as e:
            print(f"[ZIP EXTRACT ERROR] {e}")
    elif file_filename.endswith(".py"):
        target_main = os.path.join(project_dir, "main.py")
        if os.path.abspath(uploaded_file_path) != os.path.abspath(target_main):
            shutil.copy(uploaded_file_path, target_main)

    entry_script = find_and_setup_entry_script(project_dir) or "main.py"

    if app_id in RUNNING_PROCESSES:
        try:
            RUNNING_PROCESSES[app_id].terminate()
            try:
                RUNNING_PROCESSES[app_id].wait(timeout=5)
            except Exception:
                RUNNING_PROCESSES[app_id].kill()
            del RUNNING_PROCESSES[app_id]
        except Exception:
            pass

    app_status = run_app_process(app_id, project_dir, entry_script)

    db.collection('apps').document(app_id).set({
        "app_name": safe_app_name,
        "username": username,
        "entry_script": entry_script,
        "status": app_status,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/start-app/{app_id}")
def start_app(request: Request, app_id: str):
    session_id = get_current_session_id(request)
    app_doc = db.collection('apps').document(app_id).get()

    if app_doc.exists:
        app_data = app_doc.to_dict()
        username = app_data.get("username")
        app_name = app_data.get("app_name")
        project_dir = os.path.join(UPLOAD_DIR, username, app_name)
        
        entry_script = find_and_setup_entry_script(project_dir) or "main.py"

        if app_id in RUNNING_PROCESSES:
            try:
                RUNNING_PROCESSES[app_id].terminate()
                del RUNNING_PROCESSES[app_id]
            except Exception:
                pass

        app_status = run_app_process(app_id, project_dir, entry_script)
        db.collection('apps').document(app_id).update({"status": app_status})

    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/reload-app/{app_id}")
def reload_app(request: Request, app_id: str):
    return start_app(request, app_id)

@app.post("/delete-app/{app_id}")
def delete_app(request: Request, app_id: str):
    session_id = get_current_session_id(request)
    if app_id in RUNNING_PROCESSES:
        try:
            RUNNING_PROCESSES[app_id].terminate()
            try:
                RUNNING_PROCESSES[app_id].wait(timeout=5)
            except Exception:
                RUNNING_PROCESSES[app_id].kill()
            del RUNNING_PROCESSES[app_id]
        except Exception:
            pass

    app_doc = db.collection('apps').document(app_id).get()
    if app_doc.exists:
        app_data = app_doc.to_dict()
        project_dir = os.path.join(UPLOAD_DIR, app_data.get("username"), app_data.get("app_name"))
        if os.path.exists(project_dir):
            shutil.rmtree(project_dir, ignore_errors=True)

    db.collection('apps').document(app_id).delete()
    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/get-app-logs/{app_id}")
def get_app_logs(app_id: str, request: Request):
    """Return live logs only when the current session owns the app (admins may view all)."""
    current_user = get_current_user(request)
    if not current_user:
        return {"logs": "Unauthorized. Please login again.", "status": "error"}

    app_doc = db.collection("apps").document(app_id).get()
    if not app_doc.exists:
        return {"logs": "Application not found.", "status": "error"}

    app_data = app_doc.to_dict()
    owner = app_data.get("username")
    if current_user != owner:
        udoc = db.collection("users").document(current_user).get()
        if not udoc.exists or udoc.to_dict().get("role") != "admin":
            return {"logs": "Access denied.", "status": "error"}

    with PROCESS_LOG_LOCK:
        live_lines = list(PROCESS_LOGS.get(app_id, []))

    # After a server restart, restore recent lines from the persistent log.
    if not live_lines:
        log_path = os.path.join(
            UPLOAD_DIR, owner or "", app_data.get("app_name", ""), "output.log"
        )
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    live_lines = f.read().splitlines()[-300:]
            except Exception as e:
                live_lines = [f"[LOG READ ERROR] {e}"]

    proc = RUNNING_PROCESSES.get(app_id)
    running = bool(proc and proc.poll() is None)
    return {
        "logs": "\n".join(live_lines[-300:]) if live_lines else "Waiting for process stdout/stderr...",
        "status": "running" if running else app_data.get("status", "stopped")
    }

@app.get("/health")
def health():
    # Render health-check endpoint.
    return {"status": "ok", "service": "Render & Firebase PaaS Cloud Hosting Engine"}

if __name__ == "__main__":
    # Render provides PORT. Local development falls back to 8000.
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False, access_log=True)
