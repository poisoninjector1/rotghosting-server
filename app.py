import os
import shutil
import zipfile
import sqlite3
import asyncio
import docker
from datetime import datetime, timedelta
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

app = FastAPI(title="ROTG Cloud Hosting Engine")

DB_FILE = "hosting.db"
UPLOAD_DIR = os.path.abspath("./user_apps")
os.makedirs(UPLOAD_DIR, exist_ok=True)

TELEGRAM_ADMIN = "JohnRipper1337"

try:
    docker_client = docker.from_env()
except Exception:
    docker_client = None

# --- DATABASE INIT ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            max_slots INTEGER DEFAULT 1,
            slot_expiry TEXT NOT NULL,
            last_seen TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_ips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            blocked_until TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS apps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            app_name TEXT NOT NULL,
            container_name TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'stopped',
            created_at TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    ''')

    cursor.execute("SELECT * FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        one_year_expiry = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO users (username, password, role, max_slots, slot_expiry, last_seen) VALUES ('admin', 'admin123', 'admin', 999, ?, ?)", 
                       (one_year_expiry, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    
    conn.commit()
    conn.close()

init_db()

def get_client_ip(request: Request):
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0]
    return request.client.host

# --- HTML TEMPLATE WITH USER SEARCH & DURATION CONTROLS ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PaaS Cloud Host Engine</title>
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
        .btn-tg { background: #0088cc; margin-top: 0.5rem; }
        .btn-success { background: #22c55e; }
        .btn-danger { background: var(--danger-color); }
        .terminal-box { background: #020617; border: 1px solid var(--border-color); border-radius: 8px; padding: 1rem; font-family: monospace; font-size: 0.85rem; color: #38bdf8; height: 250px; overflow-y: auto; }
        .badge { display: inline-block; padding: 0.25rem 0.6rem; border-radius: 6px; font-size: 0.75rem; font-weight: bold; }
        .badge-running { background: rgba(34, 197, 94, 0.2); color: #4ade80; }
        .badge-admin { background: rgba(168, 85, 247, 0.2); color: #c084fc; }

        /* FLOATING CHAT SYSTEM */
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
        <div class="logo">PaaS Engine</div>
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
            </div>
        {% else %}
            
            {% if is_admin %}
            <!-- ADMIN DASHBOARD & STATS -->
            <h2 style="color: #c084fc; margin-bottom: 1rem;">Admin Management Dashboard</h2>
            
            <div class="grid" style="margin-bottom: 1.5rem;">
                <div class="card" style="text-align: center;">
                    <h3>Total Registered Users</h3>
                    <p style="font-size: 2rem; font-weight: bold; color: #a855f7;">{{ total_users }}</p>
                </div>
                <div class="card" style="text-align: center;">
                    <h3>Online Users (Active)</h3>
                    <p style="font-size: 2rem; font-weight: bold; color: #22c55e;">{{ online_users }}</p>
                </div>
                <div class="card" style="text-align: center;">
                    <h3>Offline Users</h3>
                    <p style="font-size: 2rem; font-weight: bold; color: #94a3b8;">{{ offline_users }}</p>
                </div>
            </div>

            <!-- ADMIN CONTROLS GRID -->
            <div class="grid" style="margin-bottom: 2rem;">
                <div class="card">
                    <h3>Change My Password</h3>
                    <form action="/admin/change-my-pass" method="post" style="margin-top: 1rem;">
                        <div class="form-group">
                            <input type="password" name="new_password" required placeholder="New Password">
                        </div>
                        <button type="submit" class="btn">Update Password</button>
                    </form>
                </div>

                <div class="card">
                    <h3>Change User Password</h3>
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
                    <h3>Give / Manage User Slots</h3>
                    <form action="/admin/manage-slots" method="post" style="margin-top: 1rem;">
                        <div class="form-group">
                            <input type="text" name="target_username" required placeholder="Target Username">
                        </div>
                        <div class="form-group">
                            <input type="number" name="slot_count" min="0" required placeholder="Slots Count (e.g. 5)">
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
                            <button type="submit" name="action" value="set" class="btn btn-success">Set Slots & Validity</button>
                            <button type="submit" name="action" value="revoke" class="btn btn-danger">Revoke</button>
                        </div>
                    </form>
                </div>
            </div>

            <!-- ALL USERS LIST & SEARCH SECTION -->
            <div class="card" style="margin-bottom: 2rem;">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <h3>Registered Users Directory</h3>
                    <form action="/" method="get" style="display: flex; gap: 8px;">
                        <input type="text" name="search_user" value="{{ search_query }}" placeholder="Search username..." style="padding: 0.5rem 0.8rem; border-radius: 6px; border: 1px solid var(--border-color); background: var(--bg-color); color: white;">
                        <button type="submit" class="btn" style="width: auto; padding: 0.5rem 1rem;">Search</button>
                        {% if search_query %}
                            <a href="/" class="btn btn-danger" style="width: auto; padding: 0.5rem 1rem;">Clear</a>
                        {% endif %}
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
                                <td style="padding: 0.8rem;"><strong>{{ u[0] }}</strong></td>
                                <td>{{ u[1] }}</td>
                                <td>{{ u[2] }}</td>
                                <td style="font-size: 0.85rem; color: #38bdf8;">{{ u[3] }}</td>
                                <td>
                                    {% if u[4] %}
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

            <!-- USER SLOT INFO -->
            <div class="card" style="margin-bottom: 1.5rem; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h3>Slot Status: {{ active_apps_count }} / {{ max_slots }} Used</h3>
                    <p style="color: var(--text-muted); font-size: 0.85rem;">Free Trial / Slot Expiry: <strong>{{ slot_expiry }}</strong></p>
                </div>
                <a href="https://t.me/{{ tg_admin }}" target="_blank" class="btn btn-tg" style="width: auto;">Buy More Slots (@{{ tg_admin }})</a>
            </div>

            <div class="grid">
                <div class="card">
                    <h3>Deploy Bot / Web App</h3>
                    <form action="/deploy" method="post" enctype="multipart/form-data" style="margin-top: 1rem;">
                        <div class="form-group">
                            <label>App Name</label>
                            <input type="text" name="app_name" placeholder="e.g. my-bot" required>
                        </div>
                        <div class="form-group">
                            <label>Upload Zip File</label>
                            <input type="file" name="zip_file" accept=".zip" required>
                        </div>
                        <button type="submit" class="btn">Deploy Project</button>
                    </form>
                </div>

                <div class="card">
                    <h3>Terminal Console</h3>
                    <div class="terminal-box" id="terminalLog">Click 'Console' on an active container to view output...</div>
                </div>
            </div>

            <div class="card" style="margin-top: 2rem;">
                <h3>Active Applications</h3>
                <div style="overflow-x: auto; margin-top: 1rem;">
                    <table style="width: 100%; border-collapse: collapse; text-align: left;">
                        <thead>
                            <tr style="border-bottom: 1px solid var(--border-color); color: var(--text-muted);">
                                <th style="padding: 0.8rem;">App Name</th>
                                <th>Container</th>
                                <th>Owner</th>
                                <th>Status</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for app in user_apps %}
                            <tr style="border-bottom: 1px solid var(--border-color);">
                                <td style="padding: 0.8rem;"><strong>{{ app[2] }}</strong></td>
                                <td style="font-family: monospace; color: #38bdf8;">{{ app[3] }}</td>
                                <td>{{ app[1] }}</td>
                                <td><span class="badge badge-running">{{ app[4] }}</span></td>
                                <td>
                                    <button class="btn" style="width: auto; padding: 0.3rem 0.6rem; font-size: 0.8rem;" onclick="connectConsole('{{ app[3] }}')">Console</button>
                                    <form action="/delete-app/{{ app[0] }}" method="post" style="display: inline;">
                                        <button class="btn btn-danger" style="width: auto; padding: 0.3rem 0.6rem; font-size: 0.8rem;">Delete</button>
                                    </form>
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" style="padding: 1rem; color: var(--text-muted); text-align: center;">No active apps found.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- FLOATING CHAT INTERFACE -->
            <div class="chat-widget">
                <button class="chat-btn" onclick="toggleChat()">💬 Chat Support</button>
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
                                <option value="{{ u[0] }}">{{ u[0] }} ({% if u[1] %}Online{% else %}Offline{% endif %})</option>
                            {% endfor %}
                        </select>
                    </div>
                    {% endif %}

                    <div class="chat-messages" id="chatMessages">
                        <div class="msg msg-other">Welcome! Send a message to get support.</div>
                    </div>

                    <div class="chat-input">
                        <input type="text" id="chatMsgInput" placeholder="Type a message..." onkeypress="if(event.key==='Enter') sendChatMessage()">
                        <button onclick="sendChatMessage()" style="background: var(--accent-color); color: white; border: none; padding: 5px 10px; border-radius: 4px; cursor: pointer;">Send</button>
                    </div>
                </div>
            </div>
        {% endif %}
    </div>

    <script>
        let activeSocket = null;
        function connectConsole(containerName) {
            const terminal = document.getElementById('terminalLog');
            terminal.innerText = "Connecting to " + containerName + "...\n";
            if(activeSocket) activeSocket.close();
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            activeSocket = new WebSocket(`${protocol}//${window.location.host}/ws/logs/${containerName}`);
            activeSocket.onmessage = function(e) { terminal.innerText += e.data; terminal.scrollTop = terminal.scrollHeight; };
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

@app.get("/", response_class=HTMLResponse)
def index_page(session_id: str = None, search_user: str = None):
    from jinja2 import Template
    current_user = ACTIVE_SESSIONS.get(session_id)
    user_apps, max_slots, slot_expiry, is_admin = [], 1, "N/A", False
    total_users, online_users, offline_users = 0, 0, 0
    all_users_list = []
    all_registered_users = []

    if current_user:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE users SET last_seen = ? WHERE username = ?", (now_str, current_user))
        conn.commit()

        cursor.execute("SELECT max_slots, slot_expiry, role FROM users WHERE username = ?", (current_user,))
        u_info = cursor.fetchone()
        if u_info:
            max_slots, slot_expiry, is_admin = u_info[0], u_info[1], (u_info[2] == 'admin')

        if is_admin:
            cursor.execute("SELECT * FROM apps ORDER BY id DESC")
            user_apps = cursor.fetchall()

            cursor.execute("SELECT username, last_seen, role, max_slots, slot_expiry FROM users")
            users_data = cursor.fetchall()
            
            non_admin_users = [u for u in users_data if u[2] != 'admin']
            total_users = len(non_admin_users)

            five_mins_ago = datetime.now() - timedelta(minutes=5)
            
            for u in users_data:
                is_online = False
                if u[1]:
                    last_seen_dt = datetime.strptime(u[1], "%Y-%m-%d %H:%M:%S")
                    if last_seen_dt > five_mins_ago:
                        is_online = True
                
                if u[2] != 'admin':
                    if is_online:
                        online_users += 1
                    else:
                        offline_users += 1
                    all_users_list.append((u[0], is_online))

                # Filter users for search
                if search_user:
                    if search_user.lower() in u[0].lower():
                        all_registered_users.append((u[0], u[2], u[3], u[4], is_online))
                else:
                    all_registered_users.append((u[0], u[2], u[3], u[4], is_online))

        else:
            cursor.execute("SELECT * FROM apps WHERE username = ? ORDER BY id DESC", (current_user,))
            user_apps = cursor.fetchall()

        conn.close()

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
        error=None
    )

@app.post("/auth")
def authenticate(request: Request, username: str = Form(...), password: str = Form(...), action: str = Form(...)):
    client_ip = get_client_ip(request)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    if action == "register":
        cursor.execute("SELECT blocked_until FROM user_ips WHERE ip_address = ? ORDER BY id DESC LIMIT 1", (client_ip,))
        ip_record = cursor.fetchone()

        if ip_record and ip_record[0]:
            blocked_until = datetime.strptime(ip_record[0], "%Y-%m-%d %H:%M:%S")
            if datetime.now() < blocked_until:
                conn.close()
                from jinja2 import Template
                return HTMLResponse(content=Template(HTML_TEMPLATE).render(
                    current_user=None, tg_admin=TELEGRAM_ADMIN,
                    error="IP Blocked! Multiple account creations detected. Try again after 24 Hours."
                ))

        cursor.execute("SELECT registered_at FROM user_ips WHERE ip_address = ?", (client_ip,))
        if len(cursor.fetchall()) >= 1:
            block_time = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("INSERT INTO user_ips (ip_address, registered_at, blocked_until) VALUES (?, ?, ?)",
                           (client_ip, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), block_time))
            conn.commit()
            conn.close()
            from jinja2 import Template
            return HTMLResponse(content=Template(HTML_TEMPLATE).render(
                current_user=None, tg_admin=TELEGRAM_ADMIN,
                error="Account limit reached for this IP! Blocked for 24 hours."
            ))

        expiry_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("INSERT INTO users (username, password, max_slots, slot_expiry, last_seen) VALUES (?, ?, 1, ?, ?)",
                           (username, password, expiry_date, now_str))
            cursor.execute("INSERT INTO user_ips (ip_address, registered_at, blocked_until) VALUES (?, ?, NULL)",
                           (client_ip, now_str))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            from jinja2 import Template
            return HTMLResponse(content=Template(HTML_TEMPLATE).render(current_user=None, tg_admin=TELEGRAM_ADMIN, error="Username already exists!"))

    cursor.execute("SELECT username FROM users WHERE username = ? AND password = ?", (username, password))
    user = cursor.fetchone()
    conn.close()

    if user:
        session_id = f"sess_{username}"
        ACTIVE_SESSIONS[session_id] = username
        return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

    from jinja2 import Template
    return HTMLResponse(content=Template(HTML_TEMPLATE).render(current_user=None, tg_admin=TELEGRAM_ADMIN, error="Invalid Credentials!"))

# --- MESSAGING APIs ---
@app.get("/get-messages")
def get_messages(target: str):
    session_id = list(ACTIVE_SESSIONS.keys())[-1] if ACTIVE_SESSIONS else ""
    current_user = ACTIVE_SESSIONS.get(session_id)
    if not current_user:
        return {"messages": []}

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sender, receiver, message, timestamp FROM messages 
        WHERE (sender = ? AND receiver = ?) OR (sender = ? AND receiver = ?)
        ORDER BY id ASC
    """, (current_user, target, target, current_user))
    
    msgs = [{"sender": r[0], "receiver": r[1], "message": r[2], "timestamp": r[3]} for r in cursor.fetchall()]
    conn.close()
    return {"messages": msgs}

@app.post("/send-message")
def send_message(receiver: str = Form(...), message: str = Form(...)):
    session_id = list(ACTIVE_SESSIONS.keys())[-1] if ACTIVE_SESSIONS else ""
    current_user = ACTIVE_SESSIONS.get(session_id)
    if not current_user or not message.strip():
        return {"status": "error"}

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO messages (sender, receiver, message, timestamp) VALUES (?, ?, ?, ?)",
                   (current_user, receiver, message, now_str))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# --- ADMIN ACTIONS CONTROLLERS ---
@app.post("/admin/change-my-pass")
def admin_change_my_pass(new_password: str = Form(...)):
    session_id = list(ACTIVE_SESSIONS.keys())[-1] if ACTIVE_SESSIONS else ""
    current_user = ACTIVE_SESSIONS.get(session_id)
    if current_user:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password = ? WHERE username = ? AND role = 'admin'", (new_password, current_user))
        conn.commit()
        conn.close()
    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/change-user-pass")
def admin_change_user_pass(target_username: str = Form(...), new_password: str = Form(...)):
    session_id = list(ACTIVE_SESSIONS.keys())[-1] if ACTIVE_SESSIONS else ""
    current_user = ACTIVE_SESSIONS.get(session_id)
    if current_user:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE username = ?", (current_user,))
        if cursor.fetchone()[0] == 'admin':
            cursor.execute("UPDATE users SET password = ? WHERE username = ?", (new_password, target_username))
            conn.commit()
        conn.close()
    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/manage-slots")
def admin_manage_slots(target_username: str = Form(...), slot_count: int = Form(...), duration_months: int = Form(1), action: str = Form(...)):
    session_id = list(ACTIVE_SESSIONS.keys())[-1] if ACTIVE_SESSIONS else ""
    current_user = ACTIVE_SESSIONS.get(session_id)
    if current_user:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE username = ?", (current_user,))
        if cursor.fetchone()[0] == 'admin':
            if action == "revoke":
                new_slots = 0
                new_expiry = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            else:
                new_slots = slot_count
                days_to_add = duration_months * 30
                new_expiry = (datetime.now() + timedelta(days=days_to_add)).strftime("%Y-%m-%d %H:%M:%S")
            
            cursor.execute("UPDATE users SET max_slots = ?, slot_expiry = ? WHERE username = ?", (new_slots, new_expiry, target_username))
            conn.commit()
        conn.close()
    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/logout")
def logout():
    ACTIVE_SESSIONS.clear()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/deploy")
async def deploy_app(app_name: str = Form(...), zip_file: UploadFile = File(...)):
    if not ACTIVE_SESSIONS:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    username = list(ACTIVE_SESSIONS.values())[-1]
    session_id = list(ACTIVE_SESSIONS.keys())[-1]

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT max_slots, slot_expiry FROM users WHERE username = ?", (username,))
    u_data = cursor.fetchone()
    max_slots, slot_expiry = u_data[0], datetime.strptime(u_data[1], "%Y-%m-%d %H:%M:%S")

    cursor.execute("SELECT COUNT(*) FROM apps WHERE username = ?", (username,))
    active_apps_count = cursor.fetchone()[0]

    if datetime.now() > slot_expiry or active_apps_count >= max_slots:
        conn.close()
        return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

    safe_app_name = "".join(e for e in app_name if e.isalnum()).lower()
    container_name = f"app_{username}_{safe_app_name}"
    project_dir = os.path.join(UPLOAD_DIR, username, safe_app_name)
    os.makedirs(project_dir, exist_ok=True)

    zip_path = os.path.join(project_dir, zip_file.filename)
    with open(zip_path, "wb") as buffer:
        shutil.copyfileobj(zip_file.file, buffer)

    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(project_dir)

    if docker_client:
        try:
            try:
                old = docker_client.containers.get(container_name)
                old.stop()
                old.remove()
            except Exception:
                pass

            docker_client.containers.run(
                image="python:3.10-slim",
                name=container_name,
                command="sh -c 'if [ -f requirements.txt ]; then pip install -r requirements.txt; fi; python main.py'",
                volumes={project_dir: {'bind': '/app', 'mode': 'rw'}},
                working_dir="/app",
                detach=True,
                restart_policy={"Name": "always"}
            )
            app_status = "running"
        except Exception:
            app_status = "error"
    else:
        app_status = "docker_unavailable"

    cursor.execute("INSERT OR REPLACE INTO apps (username, app_name, container_name, status, created_at) VALUES (?, ?, ?, ?, ?)",
                   (username, safe_app_name, container_name, app_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/delete-app/{app_id}")
def delete_app(app_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT container_name FROM apps WHERE id = ?", (app_id,))
    rec = cursor.fetchone()
    if rec and docker_client:
        try:
            c = docker_client.containers.get(rec[0])
            c.stop()
            c.remove()
        except Exception:
            pass

    cursor.execute("DELETE FROM apps WHERE id = ?", (app_id,))
    conn.commit()
    conn.close()

    session_id = list(ACTIVE_SESSIONS.keys())[-1] if ACTIVE_SESSIONS else ""
    return RedirectResponse(url=f"/?session_id={session_id}", status_code=status.HTTP_303_SEE_OTHER)

@app.websocket("/ws/logs/{container_name}")
async def stream_logs(websocket: WebSocket, container_name: str):
    await websocket.accept()
    if not docker_client:
        await websocket.send_text("Docker Service unavailable.")
        await websocket.close()
        return

    try:
        container = docker_client.containers.get(container_name)
        for log in container.logs(stream=True, follow=True, tail=100):
            await websocket.send_text(log.decode('utf-8', errors='ignore'))
            await asyncio.sleep(0.1)
    except Exception:
        await websocket.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)