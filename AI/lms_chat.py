"""
Local multi-user web chat for LM Studio.

Setup:   pip install lmstudio flask
Run:     python lms_chat.py        (LM Studio must be running)
Open:    http://127.0.0.1:5000  or the LAN address printed at startup

Data (all under BASE):
    chat/<username>/<chat_id>.json   saved chats, one file each
    users.json                       usernames + password hashes
    User.log                         activity log (no message text)
"""
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import timedelta
from functools import wraps
from pathlib import Path

import lmstudio as lms
from flask import Flask, Response, abort, jsonify, request, session, stream_with_context
from werkzeug.security import check_password_hash, generate_password_hash

BASE = Path(os.environ.get("LMS_CHAT_HOME", Path.home() / "Work/Programming/Python/projects/AI"))
CHAT_DIR = BASE / "chat"
USERS_FILE = BASE / "users.json"
LOG_FILE = BASE / "User.log"
KEY_FILE = BASE / ".secret_key"
CHAT_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT = "You are a helpful assistant."
USERNAME_RE = re.compile(r"^[a-z0-9_-]{2,24}$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def load_secret():
    if not KEY_FILE.exists():
        KEY_FILE.write_text(secrets.token_hex(32))
        try:
            os.chmod(KEY_FILE, 0o600)
        except OSError:
            pass
    return KEY_FILE.read_text().strip()


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=1_000_000,
)

# ---------- logging ----------
logger = logging.getLogger("userlog")
logger.setLevel(logging.INFO)
logger.propagate = False
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
logger.addHandler(_fh)


def log(action, user="-", detail="", ip=None):
    ip = ip or request.remote_addr
    logger.info("%-13s | user=%s | ip=%s%s", action, user, ip, f" | {detail}" if detail else "")


# ---------- storage helpers ----------
_users_lock = threading.Lock()
_gen_lock = threading.Lock()   # LM Studio handles one generation at a time
_loaded = {}


def write_json(path, data):
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_users():
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    return {}


def user_dir(u):
    d = CHAT_DIR / u
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_chat(u, cid):
    if not ID_RE.match(cid):
        return None
    p = user_dir(u) / f"{cid}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_chat(u, cid):
    chat = read_chat(u, cid)
    if chat is None:
        abort(404)
    return chat


def save_chat(u, chat):
    chat["updated"] = time.time()
    write_json(user_dir(u) / f"{chat['id']}.json", chat)


def get_model(key):
    if key not in _loaded:
        _loaded[key] = lms.llm(key)
    return _loaded[key]


def err(msg, code=400):
    return jsonify(error=msg), code


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("user"):
            return err("Please sign in.", 401)
        return fn(*a, **kw)
    return wrapper


# ---------- auth ----------
def credentials():
    d = request.get_json(force=True, silent=True) or {}
    return str(d.get("username", "")).strip().lower(), str(d.get("password", ""))


@app.post("/api/register")
def register():
    name, pw = credentials()
    if not USERNAME_RE.match(name):
        return err("Username must be 2-24 characters: letters, numbers, - or _.")
    if len(pw) < 6:
        return err("Password must be at least 6 characters.")
    with _users_lock:
        users = read_users()
        if name in users:
            return err("That username is taken.", 409)
        users[name] = generate_password_hash(pw)
        write_json(USERS_FILE, users)
    user_dir(name)
    session.permanent = True
    session["user"] = name
    log("register", name)
    return jsonify(user=name)


@app.post("/api/login")
def login():
    name, pw = credentials()
    h = read_users().get(name)
    if not h or not check_password_hash(h, pw):
        time.sleep(0.6)  # slows down password guessing
        log("login_failed", name if USERNAME_RE.match(name) else "?")
        return err("Wrong username or password.", 401)
    session.permanent = True
    session["user"] = name
    log("login", name)
    return jsonify(user=name)


@app.post("/api/logout")
def logout():
    log("logout", session.get("user", "-"))
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def me():
    return jsonify(user=session["user"])


# ---------- models ----------
@app.get("/api/models")
@login_required
def models():
    try:
        return jsonify([m.model_key for m in lms.list_downloaded_models("llm")])
    except Exception as e:
        log("error", session["user"], f"models: {str(e)[:150]}")
        return err("Can't reach LM Studio. Is it running?", 503)


# ---------- chats ----------
@app.get("/api/chats")
@login_required
def list_chats():
    out = []
    for p in user_dir(session["user"]).glob("*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": c["id"], "title": c["title"], "updated": c["updated"]})
        except (OSError, ValueError, KeyError):
            continue
    out.sort(key=lambda c: c["updated"], reverse=True)
    return jsonify(out)


@app.post("/api/chats")
@login_required
def new_chat():
    u = session["user"]
    d = request.get_json(force=True, silent=True) or {}
    now = time.time()
    chat = {"id": uuid.uuid4().hex, "title": "New chat", "created": now,
            "updated": now, "model": str(d.get("model", "")), "messages": []}
    save_chat(u, chat)
    log("chat_new", u, f"chat={chat['id']}")
    return jsonify(chat)


@app.get("/api/chats/<cid>")
@login_required
def get_chat(cid):
    return jsonify(load_chat(session["user"], cid))


@app.patch("/api/chats/<cid>")
@login_required
def rename_chat(cid):
    u = session["user"]
    title = str((request.get_json(force=True, silent=True) or {}).get("title", "")).strip()[:80]
    if not title:
        return err("Title can't be empty.")
    chat = load_chat(u, cid)
    chat["title"] = title
    save_chat(u, chat)
    log("chat_rename", u, f"chat={cid}")
    return jsonify(ok=True)


@app.delete("/api/chats/<cid>")
@login_required
def delete_chat(cid):
    u = session["user"]
    load_chat(u, cid)
    (user_dir(u) / f"{cid}.json").unlink(missing_ok=True)
    log("chat_delete", u, f"chat={cid}")
    return jsonify(ok=True)


@app.post("/api/chats/<cid>/message")
@login_required
def message(cid):
    u, ip = session["user"], request.remote_addr
    d = request.get_json(force=True, silent=True) or {}
    text = str(d.get("content", "")).strip()
    model_key = str(d.get("model", ""))
    if not text or not model_key:
        return err("Missing message or model.")

    chat = load_chat(u, cid)
    chat["messages"].append({"role": "user", "content": text})
    chat["model"] = model_key
    if chat["title"] == "New chat" and len(chat["messages"]) == 1:
        chat["title"] = text[:48] + ("…" if len(text) > 48 else "")
    save_chat(u, chat)
    log("message", u, f"chat={cid} model={model_key!r} chars={len(text)}", ip)
    history = list(chat["messages"])

    def generate():
        reply, prediction = [], None
        with _gen_lock:
            try:
                model = get_model(model_key)
                convo = lms.Chat(SYSTEM_PROMPT)
                for m in history:
                    if m["role"] == "user":
                        convo.add_user_message(m["content"])
                    else:
                        convo.add_assistant_response(m["content"])
                prediction = model.respond_stream(convo)
                for frag in prediction:
                    reply.append(frag.content)
                    yield frag.content
            except Exception as e:
                log("error", u, str(e)[:200], ip)
                yield f"\n\n[Error: {e}]"
            finally:
                # Also runs when the client presses Stop: partial replies are kept.
                if prediction is not None:
                    try:
                        prediction.cancel()
                    except Exception:
                        pass
                out = "".join(reply).strip()
                if out:
                    c = read_chat(u, cid)
                    if c is not None:
                        c["messages"].append({"role": "assistant", "content": out})
                        save_chat(u, c)

    return Response(stream_with_context(generate()), mimetype="text/plain",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- page ----------
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Local Chat</title>
<style>
:root{
  --bg:#0e1220;--side:#121728;--panel:#181e34;--line:#252d49;--text:#e9ebf5;--muted:#8d96b6;
  --accent:#8fa8ff;--on-accent:#0b1130;--user:#1d2750;--code:#0a0e1c;--danger:#ff7b87;
  --shadow:0 10px 40px rgba(0,0,0,.45);
  --font:Inter,"Segoe UI",system-ui,-apple-system,"Helvetica Neue",sans-serif;
  --mono:ui-monospace,"SF Mono",Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme:light){:root{
  --bg:#f5f6fb;--side:#fff;--panel:#fff;--line:#e0e4f0;--text:#171b2e;--muted:#6a7290;
  --accent:#4b63e3;--on-accent:#fff;--user:#e7ebff;--code:#eef0f8;--danger:#d2394a;
  --shadow:0 10px 40px rgba(30,40,90,.15)}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font:16px/1.6 var(--font);-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;background:none;border:0}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* shared */
.logo{display:grid;place-items:center;width:30px;height:30px;border-radius:9px;background:var(--accent);color:var(--on-accent);font-size:16px;flex:none}
.brand{display:flex;align-items:center;gap:10px;padding:4px 6px;font-weight:650}
.av{display:grid;place-items:center;width:32px;height:32px;border-radius:50%;background:var(--user);font-weight:600;font-size:14px;flex:none}
.av.ai{background:none;border:1px solid var(--line);color:var(--accent);margin-top:2px}
.primary{background:var(--accent);color:var(--on-accent);padding:12px;border-radius:12px;font-weight:600}
.primary:hover{filter:brightness(1.08)}
.primary.danger{background:var(--danger);color:#fff}
.ghost{padding:9px 16px;border-radius:10px;border:1px solid var(--line);color:var(--muted)}
.ghost:hover{color:var(--text);border-color:var(--muted)}
.err{color:var(--danger);font-size:14px;min-height:1.3em;margin:0}

/* login */
.login{min-height:100dvh;display:grid;place-items:center;padding:20px}
.card{width:min(400px,100%);background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:30px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:14px}
.card h1{margin:0;font-size:22px}
.card .sub{margin:-6px 0 4px;color:var(--muted);font-size:15px}
.seg{display:grid;grid-template-columns:1fr 1fr;background:var(--bg);border-radius:12px;padding:4px}
.seg button{padding:8px;border-radius:9px;color:var(--muted)}
.seg .on{background:var(--panel);color:var(--text);box-shadow:0 0 0 1px var(--line)}
.card label{display:grid;gap:6px;font-size:14px;color:var(--muted)}
.card input{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:11px 14px;color:var(--text);font-size:16px}
.card input:focus{outline:0;border-color:var(--accent)}

/* app shell */
.app{display:flex;height:100dvh}
aside{width:288px;flex:none;background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:14px 12px;gap:12px}
.new{padding:10px 12px;border-radius:12px;background:var(--accent);color:var(--on-accent);font-weight:600}
.new:hover{filter:brightness(1.08)}
.list{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:2px;margin:0 -4px;padding:0 4px}
.item{display:flex;align-items:center;border-radius:10px}
.item:hover,.item.active{background:var(--panel)}
.item.active{box-shadow:inset 3px 0 0 var(--accent)}
.item .t{flex:1;min-width:0;text-align:left;padding:9px 10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .more{width:34px;height:34px;border-radius:8px;color:var(--muted);flex:none;font-size:18px}
.item .more:hover{color:var(--text);background:var(--line)}
.ren{flex:1;min-width:0;margin:3px 4px;padding:6px 8px;background:var(--bg);border:1px solid var(--accent);border-radius:8px;outline:0}
.none{color:var(--muted);text-align:center;font-size:14px;margin-top:20px}
.me{display:flex;align-items:center;gap:10px;padding:12px 6px 2px;border-top:1px solid var(--line)}
.me span#who{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.me .ghost{padding:6px 12px;font-size:14px}

main{flex:1;min-width:0;display:flex;flex-direction:column}
.top{display:flex;align-items:center;gap:12px;padding:10px 16px;padding-top:calc(10px + env(safe-area-inset-top));border-bottom:1px solid var(--line)}
.top h1{flex:1;min-width:0;margin:0;font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.icon{display:none;width:38px;height:38px;border-radius:10px;font-size:20px;place-items:center}
select{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 10px;max-width:46vw}
.scroll{flex:1;overflow-y:auto}
.thread{max-width:780px;margin:0 auto;padding:28px 18px 12px;display:flex;flex-direction:column;gap:26px}

/* messages */
.row.user{display:flex;justify-content:flex-end}
.bubble{max-width:85%;background:var(--user);padding:10px 16px;border-radius:18px 18px 4px 18px;white-space:pre-wrap;overflow-wrap:anywhere}
.row.ai{display:flex;gap:14px;align-items:flex-start}
.body{flex:1;min-width:0;overflow-wrap:anywhere}
.body.err{color:var(--danger)}
.body>:first-child{margin-top:0}
.body p{margin:0 0 .8em}
.body h2,.body h3,.body h4,.body h5{margin:1.2em 0 .5em;line-height:1.3}
.body ul,.body ol{padding-left:1.4em;margin:0 0 .8em}
.body code{font-family:var(--mono);font-size:.9em}
.body :not(pre)>code{background:var(--code);padding:2px 6px;border-radius:6px}
.code{border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:.9em 0;background:var(--code)}
.code-h{display:flex;justify-content:space-between;align-items:center;padding:5px 8px 5px 14px;color:var(--muted);font-size:13px;border-bottom:1px solid var(--line)}
.copy{padding:4px 10px;border-radius:8px;color:var(--muted);font-size:13px}
.copy:hover{background:var(--line);color:var(--text)}
.code pre{margin:0;padding:14px;overflow-x:auto}
.streaming>:last-child::after{content:"";display:inline-block;width:8px;height:1.05em;background:var(--accent);border-radius:2px;margin-left:4px;vertical-align:text-bottom;animation:blink 1s steps(2,start) infinite}
.dots{display:inline-flex;gap:5px;padding:9px 0}
.dots i{width:7px;height:7px;border-radius:50%;background:var(--muted);animation:pulse 1.2s infinite}
.dots i:nth-child(2){animation-delay:.15s}.dots i:nth-child(3){animation-delay:.3s}
@keyframes blink{50%{opacity:0}}
@keyframes pulse{0%,60%,100%{opacity:.25}30%{opacity:1}}

.empty{text-align:center;margin:11vh auto 0;max-width:520px}
.empty .logo{width:54px;height:54px;border-radius:17px;font-size:26px;margin:0 auto 18px}
.empty h2{margin:0 0 22px;font-size:26px;line-height:1.25}
.chips{display:flex;flex-wrap:wrap;gap:10px;justify-content:center}
.chip{border:1px solid var(--line);padding:9px 16px;border-radius:999px;color:var(--muted)}
.chip:hover{color:var(--text);border-color:var(--accent)}

/* composer */
.composer{padding:8px 18px calc(14px + env(safe-area-inset-bottom))}
.box{max-width:780px;margin:0 auto;display:flex;align-items:flex-end;gap:8px;background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:8px 8px 8px 18px;box-shadow:var(--shadow);transition:border-color .15s}
.box:focus-within{border-color:var(--accent)}
textarea{flex:1;resize:none;background:none;border:0;outline:0;padding:8px 0;max-height:200px;line-height:1.5;min-width:0}
.send{width:40px;height:40px;border-radius:50%;background:var(--accent);color:var(--on-accent);display:grid;place-items:center;flex:none}
.send.stop{background:var(--danger);color:#fff}
.hint{max-width:780px;margin:8px auto 0;color:var(--muted);font-size:12.5px;text-align:center}

/* overlays */
.menu{position:fixed;z-index:30;background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:4px;min-width:150px;display:flex;flex-direction:column}
.menu button{padding:9px 12px;text-align:left;border-radius:8px}
.menu button:hover{background:var(--line)}
.menu .danger{color:var(--danger)}
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:18px;padding:22px;width:min(360px,90vw);box-shadow:var(--shadow)}
dialog::backdrop{background:rgba(5,8,20,.6)}
dialog h3{margin:0 0 6px}dialog p{margin:0 0 18px;color:var(--muted)}
.btns{display:flex;justify-content:flex-end;gap:10px}
.btns .primary{padding:9px 18px}
.toast{position:fixed;left:50%;bottom:96px;transform:translate(-50%,16px);background:var(--panel);border:1px solid var(--line);padding:10px 16px;border-radius:12px;opacity:0;pointer-events:none;transition:.2s;z-index:50;box-shadow:var(--shadow);max-width:90vw}
.toast.on{opacity:1;transform:translate(-50%,0)}
.scrim{display:none}

@media (max-width:760px){
  .icon{display:grid}
  aside{position:fixed;z-index:20;inset:0 auto 0 0;width:min(320px,86vw);transform:translateX(-102%);transition:transform .22s;padding-top:calc(14px + env(safe-area-inset-top))}
  aside.open{transform:none;box-shadow:var(--shadow)}
  .scrim{display:block;position:fixed;inset:0;background:rgba(5,8,20,.55);z-index:15;opacity:0;pointer-events:none;transition:opacity .2s}
  .scrim.on{opacity:1;pointer-events:auto}
  .hint{display:none}
  .thread{padding:20px 14px 8px;gap:22px}
  .row.ai{gap:10px}
  .bubble{max-width:92%}
  .composer{padding:8px 12px calc(12px + env(safe-area-inset-bottom))}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>

<div id="login" class="login" hidden>
  <form id="loginForm" class="card">
    <div class="brand"><span class="logo">✦</span><b>Local Chat</b></div>
    <h1>Welcome</h1>
    <p class="sub">Private chats with the models running on this PC.</p>
    <div class="seg">
      <button type="button" class="on" data-mode="login">Sign in</button>
      <button type="button" data-mode="register">Create account</button>
    </div>
    <label>Username<input id="u" autocomplete="username" autocapitalize="none" spellcheck="false" required></label>
    <label>Password<input id="p" type="password" autocomplete="current-password" required></label>
    <p id="loginErr" class="err" role="alert"></p>
    <button id="loginBtn" class="primary">Sign in</button>
  </form>
</div>

<div id="app" class="app" hidden>
  <div id="scrim" class="scrim"></div>
  <aside id="side">
    <div class="brand"><span class="logo">✦</span><b>Local Chat</b></div>
    <button id="newBtn" class="new" type="button">New chat</button>
    <nav id="list" class="list" aria-label="Chats"></nav>
    <div class="me">
      <span id="av" class="av"></span><span id="who"></span>
      <button id="out" class="ghost" type="button">Sign out</button>
    </div>
  </aside>
  <main>
    <header class="top">
      <button id="burger" class="icon" type="button" aria-label="Open chats">☰</button>
      <h1 id="ctitle">New chat</h1>
      <select id="model" aria-label="Model"></select>
    </header>
    <div id="scroll" class="scroll"><div id="thread" class="thread"></div></div>
    <form id="composer" class="composer">
      <div class="box">
        <textarea id="input" rows="1" placeholder="Message your model"></textarea>
        <button id="send" class="send" aria-label="Send"></button>
      </div>
      <p class="hint">Enter sends. Shift+Enter adds a new line.</p>
    </form>
  </main>
</div>

<div id="menu" class="menu" hidden>
  <button type="button" data-act="rename">Rename</button>
  <button type="button" data-act="delete" class="danger">Delete</button>
</div>
<dialog id="confirm">
  <form method="dialog">
    <h3>Delete this chat?</h3><p>It will be removed permanently.</p>
    <div class="btns"><button value="cancel" class="ghost">Cancel</button><button value="ok" class="primary danger">Delete</button></div>
  </form>
</dialog>
<div id="toast" class="toast" role="status"></div>

<script>
const $ = id => document.getElementById(id);
const SEND = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
const STOP = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
let me = null, chats = [], currentId = null, controller = null, menuId = null, mode = "login";

const esc = s => s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
function toast(msg){ const t=$("toast"); t.textContent=msg; t.classList.add("on"); clearTimeout(toast.t); toast.t=setTimeout(()=>t.classList.remove("on"),3500); }

async function api(path, method="GET", body){
  const r = await fetch(path,{method,headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});
  let data=null; try{ data=await r.json(); }catch{}
  const authPath = path==="/api/login"||path==="/api/register"||path==="/api/me";
  if(r.status===401 && !authPath) showLogin();
  if(!r.ok) throw new Error(data?.error || "Request failed ("+r.status+")");
  return data;
}

/* ---------- markdown ---------- */
const inline = s => s.replace(/`([^`]+)`/g,"<code>$1</code>").replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>").replace(/(^|[\s(])\*([^*\n]+)\*/g,"$1<em>$2</em>");
const codeBlock = b => `<div class="code"><div class="code-h"><span>${esc(b.lang||"text")}</span><button type="button" class="copy">Copy</button></div><pre><code>${esc(b.code.replace(/\n$/,""))}</code></pre></div>`;
function md(src){
  const blocks=[];
  src = src.replace(/```(\w*)\n?([\s\S]*?)(```|$)/g,(m,lang,code)=>{ blocks.push({lang,code}); return "\u0000"+(blocks.length-1)+"\u0000"; });
  const out=[]; let para=[], list=null, m;
  const flush=()=>{ if(para.length){ out.push("<p>"+inline(para.join("<br>"))+"</p>"); para=[]; } };
  const close=()=>{ if(list){ out.push("</"+list+">"); list=null; } };
  for(const line of esc(src).split("\n")){
    if(/^\u0000\d+\u0000$/.test(line.trim())){ flush(); close(); out.push(line.trim()); }
    else if((m=line.match(/^(#{1,4})\s+(.*)$/))){ flush(); close(); const n=m[1].length+1; out.push(`<h${n}>${inline(m[2])}</h${n}>`); }
    else if((m=line.match(/^\s*[-*]\s+(.*)$/))){ flush(); if(list!=="ul"){ close(); out.push("<ul>"); list="ul"; } out.push("<li>"+inline(m[1])+"</li>"); }
    else if((m=line.match(/^\s*\d+[.)]\s+(.*)$/))){ flush(); if(list!=="ol"){ close(); out.push("<ol>"); list="ol"; } out.push("<li>"+inline(m[1])+"</li>"); }
    else if(!line.trim()){ flush(); close(); }
    else { close(); para.push(line); }
  }
  flush(); close();
  return out.join("").replace(/\u0000(\d+)\u0000/g,(_,i)=>codeBlock(blocks[+i]));
}

function copyText(text){
  if(navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta=document.createElement("textarea"); ta.value=text; ta.style.position="fixed"; ta.style.opacity="0";
  document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); return Promise.resolve();
}
$("thread").addEventListener("click", e=>{
  const b=e.target.closest(".copy"); if(!b) return;
  copyText(b.closest(".code").querySelector("code").textContent).then(()=>{ b.textContent="Copied"; setTimeout(()=>b.textContent="Copy",1500); });
});

/* ---------- thread ---------- */
const thread=$("thread"), scroller=$("scroll"), input=$("input"), send=$("send"), modelSel=$("model");
function addUser(text){ const r=document.createElement("div"); r.className="row user"; const b=document.createElement("div"); b.className="bubble"; b.textContent=text; r.appendChild(b); thread.appendChild(r); }
function addAI(text){
  const r=document.createElement("div"); r.className="row ai";
  r.innerHTML='<div class="av ai">✦</div><div class="body"></div>';
  const body=r.querySelector(".body"); if(text) body.innerHTML=md(text);
  thread.appendChild(r); return body;
}
const toBottom = () => { scroller.scrollTop = scroller.scrollHeight; };
function showEmpty(){
  thread.innerHTML=`<div class="empty"><div class="logo">✦</div><h2>Hi ${esc(me)}, what should we work on?</h2>
  <div class="chips"><button class="chip" type="button">Explain a concept simply</button><button class="chip" type="button">Help me debug some code</button><button class="chip" type="button">Draft a message for me</button></div></div>`;
}
thread.addEventListener("click", e=>{ const c=e.target.closest(".chip"); if(c){ input.value=c.textContent+": "; input.focus(); } });
function setBusy(busy){ send.innerHTML=busy?STOP:SEND; send.classList.toggle("stop",busy); send.setAttribute("aria-label",busy?"Stop":"Send"); modelSel.disabled=busy; }

/* ---------- chats list ---------- */
const list=$("list");
async function loadChats(){ chats=await api("/api/chats"); renderList(); }
function renderList(){
  list.innerHTML="";
  if(!chats.length){ list.innerHTML='<p class="none">No chats yet.</p>'; return; }
  for(const c of chats){
    const it=document.createElement("div"); it.className="item"+(c.id===currentId?" active":""); it.dataset.id=c.id;
    it.innerHTML='<button class="t" type="button"></button><button class="more" type="button" aria-label="Chat options">⋯</button>';
    it.querySelector(".t").textContent=c.title; list.appendChild(it);
  }
}
list.addEventListener("click", e=>{
  const it=e.target.closest(".item"); if(!it) return;
  const more=e.target.closest(".more");
  if(more){ e.stopPropagation(); openMenu(it.dataset.id, more); }
  else if(e.target.closest(".t")) selectChat(it.dataset.id);
});

async function selectChat(id){
  try{
    const c=await api("/api/chats/"+id);
    currentId=id; $("ctitle").textContent=c.title; thread.innerHTML="";
    c.messages.forEach(m=>m.role==="user"?addUser(m.content):addAI(m.content));
    if(c.model && [...modelSel.options].some(o=>o.value===c.model)) modelSel.value=c.model;
    renderList(); closeDrawer(); toBottom();
  }catch(e){ toast(e.message); }
}
function newChat(){ currentId=null; $("ctitle").textContent="New chat"; showEmpty(); renderList(); closeDrawer(); input.focus(); }

/* ---------- menu / rename / delete ---------- */
const menu=$("menu");
function openMenu(id, btn){
  menuId=id; const r=btn.getBoundingClientRect();
  menu.hidden=false; menu.style.top=(r.bottom+4)+"px"; menu.style.left=Math.max(8,Math.min(r.left,innerWidth-menu.offsetWidth-8))+"px";
}
document.addEventListener("click", e=>{ if(!e.target.closest("#menu")) menu.hidden=true; });
document.addEventListener("keydown", e=>{ if(e.key==="Escape") menu.hidden=true; });
menu.addEventListener("click", e=>{
  const act=e.target.dataset.act; if(!act) return; menu.hidden=true;
  act==="rename"?startRename(menuId):deleteChat(menuId);
});
function startRename(id){
  const it=list.querySelector(`[data-id="${id}"]`); if(!it) return;
  const old=chats.find(c=>c.id===id).title;
  const inp=document.createElement("input"); inp.className="ren"; inp.value=old; inp.maxLength=80; inp.setAttribute("aria-label","Chat name");
  it.querySelector(".t").replaceWith(inp); inp.focus(); inp.select();
  let done=false;
  const finish=async save=>{
    if(done) return; done=true; const v=inp.value.trim();
    if(save && v && v!==old){ try{ await api("/api/chats/"+id,"PATCH",{title:v}); if(id===currentId) $("ctitle").textContent=v; }catch(e){ toast(e.message); } }
    loadChats();
  };
  inp.addEventListener("keydown", e=>{ if(e.key==="Enter") finish(true); if(e.key==="Escape") finish(false); });
  inp.addEventListener("blur", ()=>finish(true));
}
function confirmDelete(){
  const d=$("confirm");
  return new Promise(res=>{ d.returnValue=""; d.addEventListener("close",()=>res(d.returnValue==="ok"),{once:true}); d.showModal(); });
}
async function deleteChat(id){
  if(!await confirmDelete()) return;
  try{ await api("/api/chats/"+id,"DELETE"); if(id===currentId) newChat(); await loadChats(); }catch(e){ toast(e.message); }
}

/* ---------- drawer ---------- */
const closeDrawer=()=>{ $("side").classList.remove("open"); $("scrim").classList.remove("on"); };
$("burger").onclick=()=>{ $("side").classList.add("open"); $("scrim").classList.add("on"); };
$("scrim").onclick=closeDrawer;
$("newBtn").onclick=newChat;

/* ---------- sending ---------- */
async function sendMessage(text){
  if(!modelSel.value){ toast("No model selected. Is LM Studio running?"); return; }
  try{
    if(!currentId){ const c=await api("/api/chats","POST",{model:modelSel.value}); currentId=c.id; }
  }catch(e){ toast(e.message); return; }
  thread.querySelector(".empty")?.remove();
  addUser(text);
  const body=addAI(""); body.innerHTML='<span class="dots"><i></i><i></i><i></i></span>';
  toBottom();
  let reply="";
  controller=new AbortController(); setBusy(true);
  try{
    const r=await fetch(`/api/chats/${currentId}/message`,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({model:modelSel.value,content:text}),signal:controller.signal});
    if(r.status===401){ showLogin(); return; }
    if(!r.ok){ const d=await r.json().catch(()=>({})); throw new Error(d.error||"Request failed"); }
    const reader=r.body.getReader(), dec=new TextDecoder();
    body.classList.add("streaming");
    while(true){
      const {done,value}=await reader.read(); if(done) break;
      reply+=dec.decode(value,{stream:true});
      const near=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<140;
      body.innerHTML=md(reply); if(near) toBottom();
    }
  }catch(e){
    if(e.name!=="AbortError"){ body.classList.add("err"); body.textContent=(reply?reply+"\n\n":"")+e.message; }
    else if(!reply) body.closest(".row").remove();
  }finally{
    body.classList.remove("streaming"); controller=null; setBusy(false);
    if(me) { input.focus(); loadChats(); }
  }
}
$("composer").addEventListener("submit", e=>{
  e.preventDefault();
  if(controller){ controller.abort(); return; }
  const text=input.value.trim(); if(!text) return;
  input.value=""; input.style.height="auto"; sendMessage(text);
});
input.addEventListener("keydown", e=>{ if(e.key==="Enter" && !e.shiftKey && !e.isComposing){ e.preventDefault(); $("composer").requestSubmit(); } });
input.addEventListener("input", ()=>{ input.style.height="auto"; input.style.height=Math.min(input.scrollHeight,200)+"px"; });
modelSel.addEventListener("change", ()=>localStorage.setItem("model",modelSel.value));

/* ---------- auth / boot ---------- */
function showLogin(){
  me=null; if(controller) controller.abort();
  $("app").hidden=true; $("login").hidden=false; $("u").focus();
}
async function enterApp(user){
  me=user; $("login").hidden=true; $("app").hidden=false;
  $("who").textContent=user; $("av").textContent=user[0].toUpperCase();
  setBusy(false); newChat();
  try{
    const m=await api("/api/models");
    modelSel.innerHTML=m.map(k=>`<option>${esc(k)}</option>`).join("");
    const s=localStorage.getItem("model"); if(s && m.includes(s)) modelSel.value=s;
  }catch(e){ modelSel.innerHTML='<option value="">No models</option>'; toast(e.message); }
  loadChats().catch(()=>{});
}
document.querySelectorAll(".seg button").forEach(b=>b.onclick=()=>{
  mode=b.dataset.mode;
  document.querySelectorAll(".seg button").forEach(x=>x.classList.toggle("on",x===b));
  $("loginBtn").textContent=mode==="login"?"Sign in":"Create account";
  $("p").autocomplete=mode==="login"?"current-password":"new-password"; $("loginErr").textContent="";
});
$("loginForm").addEventListener("submit", async e=>{
  e.preventDefault(); $("loginErr").textContent="";
  try{ const d=await api("/api/"+mode,"POST",{username:$("u").value,password:$("p").value}); $("p").value=""; enterApp(d.user); }
  catch(err){ $("loginErr").textContent=err.message; }
});
$("out").onclick=async()=>{ try{ await api("/api/logout","POST"); }catch{} showLogin(); };

api("/api/me").then(d=>enterApp(d.user)).catch(showLogin);
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no traffic is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    PORT = 5000
    logger.info("server_start   | base=%s", BASE)
    print(f"\n  On this PC:    http://127.0.0.1:{PORT}")
    print(f"  On your phone: http://{lan_ip()}:{PORT}  (same Wi-Fi)")
    print(f"  Chats: {CHAT_DIR}\n  Log:   {LOG_FILE}\n")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
