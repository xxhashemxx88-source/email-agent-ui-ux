"""
tools.py — every tool the email agent is allowed to use.

note: A "tool" = a Python function + a description. The LLM reads the
      description to decide WHEN to call it, and the type hints tell it
      WHAT arguments to pass. The @tool decorator does all the wiring.

Tool list:
  read_contacts     — load the contacts sheet (Excel or CSV)
  search_knowledge  — RAG: keyword-search your knowledge/ documents
  list_files        — see what files exist in attachments/
  ask_user          — pause and ask the human a question in the terminal
  save_draft        — write an email to drafts/ (the safe default)
  send_email        — really send (SMTP, any provider, with attachments)
  read_inbox        — read the newest emails from your inbox (IMAP)
  get_current_date  — real current date/time
  Search            — DuckDuckGo web search
"""

import os
import json
import re
import glob
import smtplib
import imaplib
import mimetypes
import threading
import email as email_lib
from email.header import decode_header
from datetime import datetime
from email.message import EmailMessage

import pandas as pd
from rank_bm25 import BM25Okapi
from langchain_core.tools import tool, Tool
from langchain_community.tools import DuckDuckGoSearchRun

# note: project folders — created automatically when needed.
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
DRAFTS_DIR = os.path.join(BASE_DIR, "drafts")
ATTACH_DIR = os.path.join(BASE_DIR, "attachments")
KNOWLEDGE_DIR = os.path.join(BASE_DIR, "knowledge")
DEFAULT_SHEET = os.path.join(DATA_DIR, "contacts.xlsx")


# ---------------------------------------------------------------
# Email provider presets — set EMAIL_PROVIDER in .env and the right
# servers are picked automatically. SMTP_HOST/IMAP_HOST override them.
# note: "outlook" personal accounts (@hotmail/@outlook.com) no longer
#       accept password logins at all — kept here for completeness.
# ---------------------------------------------------------------
PROVIDERS = {
    "gmail":     {"smtp": ("smtp.gmail.com", 587),        "imap": "imap.gmail.com"},
    "outlook":   {"smtp": ("smtp-mail.outlook.com", 587), "imap": "outlook.office365.com"},
    "office365": {"smtp": ("smtp.office365.com", 587),    "imap": "outlook.office365.com"},
    "yahoo":     {"smtp": ("smtp.mail.yahoo.com", 587),   "imap": "imap.mail.yahoo.com"},
    "icloud":    {"smtp": ("smtp.mail.me.com", 587),      "imap": "imap.mail.me.com"},
    "zoho":      {"smtp": ("smtp.zoho.com", 587),         "imap": "imap.zoho.com"},
}


def _email_config() -> dict:
    """note: one place that decides which servers + credentials to use.
    Priority: explicit SMTP_HOST/SMTP_PORT/IMAP_HOST in .env win,
    otherwise the EMAIL_PROVIDER preset (default: gmail)."""
    provider = (os.getenv("EMAIL_PROVIDER") or "gmail").strip().lower()
    preset = PROVIDERS.get(provider, PROVIDERS["gmail"])
    return {
        "smtp_host": os.getenv("SMTP_HOST") or preset["smtp"][0],
        "smtp_port": int(os.getenv("SMTP_PORT") or preset["smtp"][1]),
        "imap_host": os.getenv("IMAP_HOST") or preset["imap"],
        "user": os.getenv("SMTP_USER"),
        "password": os.getenv("SMTP_PASS"),
    }


def _resolve_attachment(path: str) -> str | None:
    """note: accept either a bare filename (looked up in attachments/)
    or a full path. Returns None when the file doesn't exist."""
    path = path.strip().strip('"')
    if not path:
        return None
    if os.path.exists(path):
        return path
    candidate = os.path.join(ATTACH_DIR, path)
    return candidate if os.path.exists(candidate) else None


# note: catches unfilled template placeholders like "[Your Name]",
#       "{{Company}}" or "<Recipient>" — these must never reach a real
#       recipient. This is a deterministic backstop, not a style choice
#       left to the model: send_email refuses to send while any remain.
#       The <...> arm excludes '@' so a legit "<omar@example.com>" never
#       gets flagged as a placeholder.
_PLACEHOLDER_RE = re.compile(
    r"\[[A-Za-z][^\[\]]{0,60}\]|\{\{[^{}]{1,60}\}\}|<[A-Za-z][^<>@]{0,60}>"
)


def _find_placeholders(*texts: str) -> list[str]:
    found = []
    for t in texts:
        found.extend(_PLACEHOLDER_RE.findall(t))
    return found


# ---------------------------------------------------------------
# Human-interaction channel — ask_user and the send confirmation go
# through here. Default: the terminal. The web server (server.py)
# swaps in a per-thread handler that routes the question to the
# browser instead, so parallel sessions never collide.
# ---------------------------------------------------------------
_ask_ctx = threading.local()


def _terminal_ask(question: str, kind: str = "question", meta: dict | None = None) -> str:
    """Default handler: print to the terminal and read the typed answer."""
    if kind == "confirm_send":
        m = meta or {}
        print("\n" + "=" * 60)
        print("READY TO SEND — REVIEW BEFORE CONFIRMING")
        print("=" * 60)
        print(f"To:      {m.get('to', '')}")
        if m.get("cc"):
            print(f"Cc:      {m['cc']}")
        if m.get("bcc"):
            print(f"Bcc:     {m['bcc']}")
        print(f"Subject: {m.get('subject', '')}")
        if m.get("attachments"):
            print(f"Attach:  {m['attachments']}")
        print("-" * 60)
        print(m.get("body", ""))
        print("=" * 60)
        prompt_txt = "Type exactly 'yes' to send this, anything else cancels: "
    else:
        print(f"\n[AGENT ASKS] {question}")
        prompt_txt = "Your answer: "
    try:
        return input(prompt_txt).strip()
    except EOFError:
        # note: happens when running non-interactively — never treat as YES.
        return ""


def set_ask_handler(fn) -> None:
    """Route human questions to a custom channel (e.g. a web session).
    The handler applies to the CURRENT THREAD only; signature must be
    fn(question: str, kind: str, meta: dict | None) -> str."""
    _ask_ctx.handler = fn


def _ask_human(question: str, kind: str = "question", meta: dict | None = None) -> str:
    handler = getattr(_ask_ctx, "handler", None) or _terminal_ask
    return handler(question, kind, meta)


# ---------------------------------------------------------------
# Tool: read the contacts sheet (Excel or CSV)
# ---------------------------------------------------------------
@tool
def read_contacts(sheet_path: str = "") -> str:
    """Read the contacts sheet and return ALL rows as JSON.
    Use this whenever the user targets a group of people (a condition,
    a company, a city, 'everyone', ...). After reading, YOU filter the
    rows by the user's condition. Works with .xlsx and .csv files.
    Leave sheet_path empty to use the default data/contacts.xlsx."""
    path = sheet_path or DEFAULT_SHEET
    if not os.path.exists(path):
        return f"ERROR: no contacts file found at {path}"
    # note: pick the reader by file extension.
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)
    return json.dumps(df.to_dict(orient="records"), ensure_ascii=False, default=str)


# ---------------------------------------------------------------
# Tool: RAG — search the knowledge/ folder
# ---------------------------------------------------------------
@tool
def search_knowledge(query: str) -> str:
    """Search the user's own documents (knowledge/ folder) for facts about
    their company, products, policies, prices or email templates. Use this
    BEFORE writing any email that mentions specific facts about the user's
    business — never invent such facts. Input: a short search query."""
    files = glob.glob(os.path.join(KNOWLEDGE_DIR, "*.md")) + glob.glob(
        os.path.join(KNOWLEDGE_DIR, "*.txt")
    )
    if not files:
        return "The knowledge folder is empty — no documents to search."

    # note: this is a LIGHT RAG. Every paragraph of every file becomes a
    #       "chunk"; BM25 ranks chunks by keyword relevance to the query.
    chunks, sources = [], []
    for fp in files:
        with open(fp, "r", encoding="utf-8") as f:
            text = f.read()
        for para in text.split("\n\n"):
            para = para.strip()
            if len(para) > 20:
                chunks.append(para)
                sources.append(os.path.basename(fp))
    if not chunks:
        return "The knowledge documents are empty."

    tokenized = [c.lower().split() for c in chunks]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(query.lower().split())
    # note: take the 3 best-scoring chunks, best first.
    top = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)[:3]
    results = [
        f"[from {sources[i]}]\n{chunks[i]}" for i in top if scores[i] > 0
    ]
    return "\n\n---\n\n".join(results) if results else "No relevant information found in the knowledge folder."


# ---------------------------------------------------------------
# Tool: list files available to attach
# ---------------------------------------------------------------
@tool
def list_files() -> str:
    """List the files inside the attachments/ folder that can be attached
    to an email. Call this before offering the user an attachment, so you
    only suggest files that really exist."""
    os.makedirs(ATTACH_DIR, exist_ok=True)
    entries = []
    for name in os.listdir(ATTACH_DIR):
        full = os.path.join(ATTACH_DIR, name)
        if os.path.isfile(full):
            entries.append({"file": name, "size_kb": round(os.path.getsize(full) / 1024, 1)})
    return json.dumps(entries, ensure_ascii=False) if entries else "The attachments folder is empty."


# ---------------------------------------------------------------
# Tool: ask the human a question (human-in-the-loop)
# ---------------------------------------------------------------
@tool
def ask_user(question: str) -> str:
    """Ask the human user ONE short question and get their typed answer.
    Use it when required information is missing (recipient, purpose, ...),
    to offer attachments, and to CONFIRM before really sending emails.
    Keep questions short and specific."""
    answer = _ask_human(question)
    if not answer:
        # note: no answer (EOF, timeout, closed tab) must never mean YES.
        return "(no answer from user — treat as NO / do not send; prefer saving a draft)"
    # note: verbose=True reprints every tool's return value to the terminal.
    #       Without a label, that echo looks like a confusing duplicate of
    #       what you just typed — this prefix makes it read as a log line.
    return f"(user answered) {answer}"


# ---------------------------------------------------------------
# Tool: save a draft (the safe default action)
# ---------------------------------------------------------------
@tool
def save_draft(to: str, subject: str, body: str, cc: str = "", attachments: str = "") -> str:
    """Save ONE email as a draft file on disk instead of sending it.
    This is the DEFAULT action — always prefer it unless the user
    explicitly asked to send. 'attachments' is an optional comma-separated
    list of file names from the attachments folder (or full paths)."""
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    safe_to = re.sub(r"[^\w.@-]", "_", to).replace("@", "_at_")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(DRAFTS_DIR, f"{safe_to}_{stamp}.txt")

    attach_note = ""
    if attachments.strip():
        attach_note = f"Attachments: {attachments}\n"
    header = f"To: {to}\n" + (f"Cc: {cc}\n" if cc.strip() else "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{header}Subject: {subject}\n{attach_note}\n{body}")
    return f"Draft saved: {path}"


# ---------------------------------------------------------------
# Tool: really send an email (SMTP — any provider)
# ---------------------------------------------------------------
@tool
def send_email(to: str, subject: str, body: str, cc: str = "", bcc: str = "", attachments: str = "") -> str:
    """Send ONE real email immediately. Only use this when the user
    EXPLICITLY asked to send. 'to', 'cc' and 'bcc' can hold several
    addresses separated by commas. 'attachments' is an optional
    comma-separated list of file names from the attachments folder (or
    full paths). Before anything is actually sent, the human is SHOWN
    the exact final text and must confirm it — this happens
    automatically in code and cannot be skipped. If the human declines,
    or if sending fails for any reason, the email is saved as a draft
    instead so nothing is lost or sent unreviewed. Never tell the user
    to go type something in a terminal — the confirmation prompt is
    presented to them right where they are."""
    # note: HARD GATE — no unfilled placeholder text can reach send_email,
    #       regardless of whether the model remembered to resolve it.
    placeholders = _find_placeholders(subject, body)
    if placeholders:
        return (
            f"NOT SENT: the email still contains unfilled placeholder text: {placeholders}. "
            "Resolve every one of them — use information you already have "
            "(e.g. the sender identity given in your system instructions, "
            "the recipient's name from contacts, today's date) or call "
            "ask_user for anything genuinely unknown — then call send_email again."
        )

    cfg = _email_config()
    if not cfg["user"] or not cfg["password"]:
        result = save_draft.func(to=to, subject=subject, body=body, cc=cc, attachments=attachments)
        return (
            "Email credentials are not configured (SMTP_USER / SMTP_PASS "
            f"missing in .env), so nothing was sent. {result}"
        )

    # note: resolve attachments BEFORE the human confirms — confirming an
    #       email that then fails on a missing file would waste the review.
    resolved, missing = [], []
    for raw in [a for a in attachments.split(",") if a.strip()]:
        real = _resolve_attachment(raw)
        (resolved.append(real) if real else missing.append(raw.strip()))
    if missing:
        return (
            f"NOT SENT: these attachment files do not exist: {missing}. "
            "Call list_files to see what is available, fix the names and try again."
        )

    # note: HARD GATE — enforced in code, not left to the model's judgment.
    #       The exact final text is shown and a literal "yes" is required
    #       before anything goes out, no matter what was agreed earlier.
    #       CONFIRM_SEND=0 (settable from the web settings page) skips it.
    if (os.getenv("CONFIRM_SEND") or "1").strip() != "0":
        confirm = _ask_human(
            "Send this email now?",
            kind="confirm_send",
            meta={"to": to, "cc": cc.strip(), "bcc": bcc.strip(),
                  "subject": subject, "attachments": attachments.strip(),
                  "body": body},
        )
        if confirm.lower() != "yes":
            result = save_draft.func(to=to, subject=subject, body=body, cc=cc, attachments=attachments)
            return f"NOT SENT — human did not confirm. Saved as draft instead. {result}"

    msg = EmailMessage()
    msg["From"] = cfg["user"]
    msg["To"] = to
    if cc.strip():
        msg["Cc"] = cc
    if bcc.strip():
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg.set_content(body)

    for real in resolved:
        ctype, _ = mimetypes.guess_type(real)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(real, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                               filename=os.path.basename(real))

    # note: any SMTP failure must NOT crash the agent — fall back to a
    #       draft so the written email is never lost.
    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as server:
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        sent_to = to + (f" (cc: {cc})" if cc.strip() else "") + (f" (bcc: {bcc})" if bcc.strip() else "")
        return f"Email sent to {sent_to}" + (f" with attachments: {attachments}" if attachments.strip() else "")
    except Exception as e:
        result = save_draft.func(to=to, subject=subject, body=body, cc=cc, attachments=attachments)
        return (
            f"SENDING FAILED ({type(e).__name__}: {e}). "
            f"The email was saved as a draft instead. {result}"
        )


# ---------------------------------------------------------------
# Tool: read the inbox (IMAP)
# ---------------------------------------------------------------
def _decode(value: str | None) -> str:
    """note: email headers arrive encoded (e.g. '=?utf-8?...?='); this
    turns them back into normal readable text."""
    if not value:
        return ""
    parts = decode_header(value)
    out = ""
    for text, charset in parts:
        out += text.decode(charset or "utf-8", errors="replace") if isinstance(text, bytes) else text
    return out


@tool
def read_inbox(limit: int = 5, from_filter: str = "") -> str:
    """Read the NEWEST emails from the user's own inbox and return them as
    JSON (from, subject, date, snippet). Use when the user asks about
    received emails, replies, or 'check my inbox'. 'from_filter' optionally
    limits results to a sender address. Does not mark anything as read."""
    cfg = _email_config()
    if not cfg["user"] or not cfg["password"]:
        return "ERROR: SMTP_USER / SMTP_PASS are not configured in .env."
    try:
        box = imaplib.IMAP4_SSL(cfg["imap_host"], 993)
        box.login(cfg["user"], cfg["password"])
        box.select("INBOX", readonly=True)  # note: readonly = nothing gets marked as read
        criteria = f'(FROM "{from_filter}")' if from_filter.strip() else "ALL"
        _, data = box.search(None, criteria)
        ids = data[0].split()
        results = []
        for msg_id in reversed(ids[-max(1, min(limit, 20)):]):
            _, fetched = box.fetch(msg_id, "(BODY.PEEK[])")
            if not fetched or not fetched[0] or not isinstance(fetched[0], tuple):
                continue  # note: server returned no body for this id — skip it
            raw = email_lib.message_from_bytes(fetched[0][1])
            # note: find the plain-text part and keep a short snippet.
            snippet = ""
            for part in raw.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        snippet = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                        break
            results.append({
                "from": _decode(raw.get("From")),
                "subject": _decode(raw.get("Subject")),
                "date": raw.get("Date", ""),
                "snippet": " ".join(snippet.split())[:400],
            })
        box.logout()
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return f"ERROR reading inbox ({type(e).__name__}): {e}"


# ---------------------------------------------------------------
# Tool: today's real date and time
# ---------------------------------------------------------------
@tool
def get_current_date() -> str:
    """Get today's REAL date and local time (with UTC offset). Always call
    this before anything time-sensitive. Do NOT trust dates inside search
    results — 'today' or '1 day ago' there refer to when the article was
    written, not to the actual current date."""
    return datetime.now().astimezone().strftime("%A, %Y-%m-%d %H:%M (UTC%z)")


# ---------------------------------------------------------------
# Tool: web search
# ---------------------------------------------------------------
search = DuckDuckGoSearchRun()
search_tool = Tool(
    name="Search",
    func=search.run,
    description=(
        "Search the web. Use for public facts and current events you are "
        "not sure about. Input is a search query string."
    ),
)


# note: main.py imports this list — add or remove tools in ONE place.
ALL_TOOLS = [
    read_contacts,
    search_knowledge,
    list_files,
    ask_user,
    save_draft,
    send_email,
    read_inbox,
    get_current_date,
    search_tool,
]
