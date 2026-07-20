# claude-email-agent

A general-purpose AI email agent. It can write, personalize, and batch
emails to any group you describe from a contacts sheet, ground the
content in your own documents (light RAG), attach files (asking you
first), read your inbox, and **save drafts** by default — really sending
only when you explicitly say "send" **and** confirm.

Two ways to use it: the **AgentDock web UI** (`python server.py`) or the
plain **terminal session** (`python main.py`). Both run the same agent.

## Project structure

```
claude-email-agent/
├── main.py            # terminal entry point (chat loop + printed report)
├── server.py          # web entry point: FastAPI + WebSocket bridge (port 8000)
├── agent_core.py      # the shared brain: LLM + system prompt + structured report
├── tools.py           # all 9 tools (see below)
├── frontend/
│   ├── index.html     # AgentDock UI (dark/light, EN/AR, live tool timeline)
│   └── AgentDock.html # the original design prototype (reference only)
├── data/
│   ├── contacts.xlsx        # your contacts sheet (.xlsx or .csv, any columns)
│   └── agent_settings.json  # web-UI settings (created on first save)
├── knowledge/         # RAG: drop .md/.txt docs about your business here
├── attachments/       # files the agent may attach to emails
├── drafts/            # drafted emails are saved here as .txt files
├── requirements.txt
└── .env               # keys and email credentials (never commit this)
```

## Tools

| Tool | What it does |
|---|---|
| `read_contacts` | loads the contacts sheet (xlsx/csv) as JSON |
| `search_knowledge` | RAG keyword search over `knowledge/` docs |
| `list_files` | lists what's inside `attachments/` |
| `ask_user` | asks YOU a question mid-run (missing info, attachment offers, send confirmation) |
| `save_draft` | writes an email to `drafts/` — the default action |
| `send_email` | really sends (any provider, cc/bcc/attachments, falls back to draft on failure) |
| `read_inbox` | reads your newest emails over IMAP (read-only) |
| `get_current_date` | real current date/time |
| `Search` | DuckDuckGo web search |

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`.env`:

```
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=deepseek/deepseek-v4-flash

# gmail / outlook / office365 / yahoo / icloud / zoho — or set
# SMTP_HOST / SMTP_PORT / IMAP_HOST manually for a custom server.
EMAIL_PROVIDER=gmail
SMTP_USER=you@gmail.com
SMTP_PASS=your-app-password
```

> Gmail/Yahoo/iCloud/Zoho need an **app password** (enable 2FA first).
> Personal Outlook/Hotmail accounts no longer accept password logins at
> all — use another provider or the Microsoft Graph API.

## Usage

### Web UI (AgentDock)

```powershell
python server.py
```

Open <http://localhost:8000>. Write a prompt, pick attachments, hit
**Run agent** — the tool timeline streams live, the agent's questions
appear as cards you answer in the chat (Yes/No or free text), and
sending shows the exact final email for your confirmation first.
The **.env panel** edits your real `.env`; **Settings** controls the
model, system prompt, tool toggles, identity and safety switches.
The server binds to 127.0.0.1 only — nothing is exposed to the network.

### Terminal

```powershell
python main.py
```

It runs as a session — keep giving requests, type `exit` to quit.
Follow-ups work in both UIs: after "draft an invitation to everyone at
TechCorp" you can just say "now send it".

Example requests:

- `Draft a polite payment reminder for everyone whose amount_due is greater than 0`
- `Send our new Sidr honey announcement to all customers in Jeddah` *(uses knowledge/)*
- `Check my inbox and draft replies to anything that looks urgent`
- `Email the price list to omar@example.com` *(agent offers files from attachments/)*

## Safety defaults

- Drafts unless you explicitly say **send** — and sending is confirmed
  with you once per run before anything goes out.
- Sending failures never crash the run; the email falls back to `drafts/`.
- `read_inbox` is read-only (never marks messages as read).
