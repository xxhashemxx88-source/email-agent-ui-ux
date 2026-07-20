"""
server.py — the AgentDock web bridge for the email agent.

Serves the frontend (frontend/index.html) at http://localhost:8000 and
exposes the SAME agent that main.py runs in the terminal:

  REST
    GET  /api/state              everything the UI needs on load
    GET  /api/env                raw .env text
    POST /api/env                save raw .env text (reloads config)
    GET  /api/files              files in attachments/
    POST /api/attachments        upload files into attachments/
    DELETE /api/attachments/{n}  remove an uploaded file
    POST /api/settings           model / prompt / tools / identity / safety

  WebSocket /ws — one agent session per connection
    client → {"type":"run","prompt":"...","attachments":["a.pdf"]}
             {"type":"answer","text":"..."}      (reply to an ask)
             {"type":"reset"}                    (forget chat history)
    server → {"type":"state","value":"running|waiting|done"}
             {"type":"tool_start","name":...}    live tool timeline
             {"type":"tool_end","name":...,"ms":...,"output":...}
             {"type":"ask","kind":"question|confirm_send",...}
             {"type":"report",...} | {"type":"agent","text":...}
             {"type":"error","text":...}

Human-in-the-loop: tools.set_ask_handler() routes ask_user and the
send-confirmation of the CURRENT worker thread to this session's
browser, where main.py keeps using the terminal.
"""

import sys

# note: same Windows cp1252 fix as main.py — verbose agent logs may
#       contain emoji/Arabic and must not crash the server console.
sys.stdout.reconfigure(encoding="utf-8")

import os
import re
import json
import time
import queue
import asyncio
import threading

from dotenv import load_dotenv

load_dotenv()  # before agent_core import — build_agent needs the key

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.callbacks import BaseCallbackHandler

from tools import set_ask_handler, ATTACH_DIR, DRAFTS_DIR
from agent_core import build_agent, parser, DEFAULT_SYSTEM_PROMPT, DEFAULT_MODEL, TOOL_NAMES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_FILE = os.path.join(BASE_DIR, "frontend", "index.html")
ENV_PATH = os.path.join(BASE_DIR, ".env")
SETTINGS_PATH = os.path.join(BASE_DIR, "data", "agent_settings.json")

app = FastAPI(title="AgentDock — email agent")


# ---------------------------------------------------------------
# Settings: .env holds identity/provider/model/keys (single source
# of truth); agent_settings.json holds what has no .env home
# (system prompt, tool toggles, draft-default).
# ---------------------------------------------------------------
def _load_json_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_json_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _upsert_env_keys(updates: dict) -> None:
    """Update KEY=VALUE lines in .env in place (preserving comments and
    unrelated lines); append keys that don't exist yet."""
    text = ""
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        line = f"{key}={value}"
        if pattern.search(text):
            text = pattern.sub(line, text)
        else:
            text = text.rstrip("\n") + ("\n" if text.strip() else "") + line + "\n"
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    load_dotenv(ENV_PATH, override=True)


def current_settings() -> dict:
    stored = _load_json_settings()
    tools_on = {name: True for name in TOOL_NAMES}
    tools_on.update({k: bool(v) for k, v in stored.get("tools_on", {}).items() if k in tools_on})
    return {
        "model": os.getenv("OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL,
        "sysPrompt": stored.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
        "toolsOn": tools_on,
        "senderName": os.getenv("SENDER_NAME", ""),
        "provider": (os.getenv("EMAIL_PROVIDER") or "gmail").strip().lower(),
        "draftDefault": bool(stored.get("draft_default", True)),
        "confirmSend": (os.getenv("CONFIRM_SEND") or "1").strip() != "0",
    }


def list_attachments() -> list[dict]:
    os.makedirs(ATTACH_DIR, exist_ok=True)
    out = []
    for name in sorted(os.listdir(ATTACH_DIR)):
        full = os.path.join(ATTACH_DIR, name)
        if os.path.isfile(full):
            out.append({"name": name, "size_kb": round(os.path.getsize(full) / 1024, 1)})
    return out


def _read_env_text() -> str:
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


# ---------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(FRONTEND_FILE, media_type="text/html")


@app.get("/api/state")
def api_state():
    return {
        "settings": current_settings(),
        "files": list_attachments(),
        "env": _read_env_text(),
        "toolNames": TOOL_NAMES,
        "defaultModel": DEFAULT_MODEL,
        "defaultSysPrompt": DEFAULT_SYSTEM_PROMPT,
    }


@app.get("/api/env", response_class=PlainTextResponse)
def api_env_get():
    return _read_env_text()


@app.post("/api/env")
async def api_env_save(request: Request):
    text = (await request.body()).decode("utf-8")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    load_dotenv(ENV_PATH, override=True)
    return {"ok": True, "settings": current_settings()}


@app.get("/api/files")
def api_files():
    return list_attachments()


_SAFE_NAME_RE = re.compile(r"[^\w.\- ()\[\]؀-ۿ]+")


@app.post("/api/attachments")
async def api_upload(files: list[UploadFile] = File(...)):
    os.makedirs(ATTACH_DIR, exist_ok=True)
    saved = []
    for up in files:
        name = _SAFE_NAME_RE.sub("_", os.path.basename(up.filename or "file"))
        if not name.strip("._ "):
            continue
        dest = os.path.join(ATTACH_DIR, name)
        with open(dest, "wb") as f:
            f.write(await up.read())
        saved.append(name)
    return {"saved": saved, "files": list_attachments()}


@app.delete("/api/attachments/{name}")
def api_delete_attachment(name: str):
    safe = os.path.basename(name)
    full = os.path.abspath(os.path.join(ATTACH_DIR, safe))
    if not full.startswith(os.path.abspath(ATTACH_DIR)):
        return JSONResponse({"error": "invalid name"}, status_code=400)
    if os.path.isfile(full):
        os.remove(full)
    return {"ok": True, "files": list_attachments()}


@app.post("/api/settings")
async def api_settings_save(request: Request):
    body = await request.json()

    env_updates = {}
    if "model" in body:
        env_updates["OPENROUTER_MODEL"] = str(body["model"]).strip() or DEFAULT_MODEL
    if "senderName" in body:
        env_updates["SENDER_NAME"] = str(body["senderName"]).strip()
    if "provider" in body:
        env_updates["EMAIL_PROVIDER"] = str(body["provider"]).strip().lower()
    if "confirmSend" in body:
        env_updates["CONFIRM_SEND"] = "1" if body["confirmSend"] else "0"
    if env_updates:
        _upsert_env_keys(env_updates)

    stored = _load_json_settings()
    if "sysPrompt" in body:
        text = str(body["sysPrompt"]).strip()
        # note: storing None when it matches the default keeps the json
        #       clean and lets future default-prompt improvements apply.
        stored["system_prompt"] = None if text == DEFAULT_SYSTEM_PROMPT.strip() else text
    if "toolsOn" in body and isinstance(body["toolsOn"], dict):
        stored["tools_on"] = {k: bool(v) for k, v in body["toolsOn"].items() if k in TOOL_NAMES}
    if "draftDefault" in body:
        stored["draft_default"] = bool(body["draftDefault"])
    _save_json_settings(stored)

    return {"ok": True, "settings": current_settings()}


@app.get("/api/drafts")
def api_drafts():
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    out = []
    for name in sorted(os.listdir(DRAFTS_DIR), reverse=True):
        full = os.path.join(DRAFTS_DIR, name)
        if os.path.isfile(full):
            out.append({"name": name, "mtime": os.path.getmtime(full)})
    return out


# ---------------------------------------------------------------
# WebSocket: one live agent session per connection
# ---------------------------------------------------------------
class WSCallback(BaseCallbackHandler):
    """Streams every tool call to the browser timeline."""

    def __init__(self, session: "Session"):
        self.session = session
        self._starts: dict = {}

    def on_tool_start(self, serialized, input_str, *, run_id=None, **kwargs):
        name = (serialized or {}).get("name", "tool")
        self._starts[run_id] = (name, time.perf_counter())
        self.session.emit({"type": "tool_start", "name": name,
                           "input": str(input_str)[:200]})

    def _finish(self, run_id, text, error=False):
        name, t0 = self._starts.pop(run_id, ("tool", time.perf_counter()))
        self.session.emit({
            "type": "tool_end", "name": name, "error": error,
            "output": text[:280],
            "ms": int((time.perf_counter() - t0) * 1000),
        })

    def on_tool_end(self, output, *, run_id=None, **kwargs):
        self._finish(run_id, str(output))

    def on_tool_error(self, error, *, run_id=None, **kwargs):
        self._finish(run_id, f"{type(error).__name__}: {error}", error=True)


class Session:
    """State for one WebSocket connection: chat history, the outbound
    event queue, and the inbound answer queue for human-in-the-loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.out_q: asyncio.Queue = asyncio.Queue()
        self.answer_q: queue.Queue = queue.Queue()
        self.history: list = []
        self.busy = False
        self.closed = False

    def emit(self, event: dict) -> None:
        """Thread-safe: worker threads push events onto the asyncio queue."""
        if not self.closed:
            self.loop.call_soon_threadsafe(self.out_q.put_nowait, event)

    def web_ask(self, question: str, kind: str = "question", meta: dict | None = None) -> str:
        """Runs INSIDE the agent worker thread: forward the question to the
        browser, then block until the user answers (or the tab closes)."""
        self.emit({"type": "ask", "kind": kind, "question": question, "meta": meta or {}})
        self.emit({"type": "state", "value": "waiting"})
        try:
            answer = self.answer_q.get(timeout=1800)  # 30 min, then give up
        except queue.Empty:
            answer = ""
        self.emit({"type": "state", "value": "running"})
        return (answer or "").strip()

    def close(self) -> None:
        self.closed = True
        # note: unblock a worker waiting on an answer — empty string is the
        #       safe "no answer" value (never interpreted as YES).
        self.answer_q.put("")


def _run_agent(session: Session, prompt_text: str, attachments: list[str]) -> None:
    """Worker thread: run one agent turn and stream everything back."""
    set_ask_handler(session.web_ask)  # this thread's questions go to the browser
    callback = WSCallback(session)
    try:
        cfg = current_settings()
        executor = build_agent(
            model=cfg["model"],
            system_core=cfg["sysPrompt"],
            enabled_tools=[n for n, on in cfg["toolsOn"].items() if on],
            sender_name=cfg["senderName"],
            draft_default=cfg["draftDefault"],
        )

        agent_input = prompt_text
        if attachments:
            agent_input += (
                "\n\n(The user pre-selected these files from the attachments "
                f"folder for this task: {', '.join(attachments)} — attach them "
                "without asking again.)"
            )

        session.emit({"type": "state", "value": "running"})
        result = executor.invoke(
            {"input": agent_input, "chat_history": list(session.history)},
            config={"callbacks": [callback]},
        )
        output = result.get("output", "")

        try:
            report = parser.parse(output)
            session.emit({"type": "report", **report.model_dump()})
        except Exception:
            # note: model skipped the structured format — show its text as-is.
            session.emit({"type": "agent", "text": output})

        session.history.append(HumanMessage(content=prompt_text))
        session.history.append(AIMessage(content=output))
        session.emit({"type": "state", "value": "done"})
    except Exception as e:
        session.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
        session.emit({"type": "state", "value": "done"})
    finally:
        session.busy = False


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session = Session(asyncio.get_running_loop())

    async def pump_out():
        while True:
            event = await session.out_q.get()
            await ws.send_json(event)

    sender = asyncio.create_task(pump_out())
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "run":
                if session.busy:
                    session.emit({"type": "error", "text": "A run is already in progress — answer its question or wait for it to finish."})
                    continue
                prompt_text = str(msg.get("prompt", "")).strip()
                if not prompt_text:
                    continue
                session.busy = True
                attachments = [str(a) for a in msg.get("attachments", []) if a]
                threading.Thread(
                    target=_run_agent, args=(session, prompt_text, attachments),
                    daemon=True,
                ).start()
            elif mtype == "answer":
                session.answer_q.put(str(msg.get("text", "")))
            elif mtype == "reset":
                if not session.busy:
                    session.history.clear()
    except WebSocketDisconnect:
        pass
    finally:
        session.close()
        sender.cancel()


if __name__ == "__main__":
    import uvicorn

    # note: localhost only — this app edits your .env and sends your email;
    #       it must never listen on the network.
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
