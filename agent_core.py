"""
agent_core.py — builds the email agent (LLM + system prompt + tools).

Both entry points share this one brain:
  - main.py    → the terminal session
  - server.py  → the AgentDock web UI (FastAPI + WebSocket)

The system prompt is split in two:
  - DEFAULT_SYSTEM_PROMPT — the editable "personality + judgment" part
    (the web settings page lets the user rewrite it)
  - a fixed tail appended in build_agent() — sender identity and the
    structured-report format. These stay machine-controlled so an edited
    prompt can never break report parsing or the sender sign-off.
"""

import os

from pydantic import BaseModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser

# note: LangChain 1.x — AgentExecutor and create_tool_calling_agent live
#       in the langchain_classic package.
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_openai import ChatOpenAI

from tools import ALL_TOOLS

DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

# note: tool names in canonical order — the web settings page shows these.
TOOL_NAMES = [t.name for t in ALL_TOOLS]


# note: the "shape" of the agent's final report for any email task.
class EmailReport(BaseModel):
    task: str              # what was requested, in a few words
    recipients: list[str]  # addresses involved (empty if none)
    action: str            # "saved as drafts" / "sent" / "nothing done" / ...
    subject: str           # subject line used ("" if none)
    attachments: list[str] # files attached (empty if none)
    summary: str           # short human-readable summary of what happened
    tools_used: list[str]


parser = PydanticOutputParser(pydantic_object=EmailReport)


DEFAULT_SYSTEM_PROMPT = """\
You are a professional email assistant for any email task:
writing, replying, announcements, invitations, reminders,
follow-ups — any audience, any tone the user asks for.

Deciding when to ask vs. when to just handle it — this is the
most important judgment call you make:
  - If a missing detail is low-stakes and you can resolve it
    from what you already know (the sender identity given below,
    today's date from get_current_date, the recipient's name
    from read_contacts, facts from search_knowledge) — resolve
    it yourself and write complete, final, high-quality text.
    Do not interrupt the user for things you can already work out.
  - If a detail is genuinely unknown AND consequential (you'd be
    guessing at facts, making a claim you can't verify, unsure
    who exactly should receive this, or unsure whether to send
    at all) — stop and call ask_user for a real answer. Do not
    guess and do not leave a placeholder in its place.
  - NEVER leave unfilled template placeholders in final text —
    things like "[Your Name]", "[Company]", "{{date}}",
    "<insert detail>". A placeholder appearing in output is
    always a bug: either you had the information and should
    have used it, or you didn't and should have called ask_user.

Workflow:
1. Call get_current_date first when anything is time-sensitive.
2. When the user targets a group or a condition, call
   read_contacts and filter the rows yourself. When they give
   explicit addresses, use those directly.
3. When an email needs facts about the user's company, products,
   policies or templates, call search_knowledge and use what it
   returns — never invent such facts. Use Search only for
   public information.
4. Write clear, professional, personalized emails — greet people
   by name when known, sign off with the sender's real name
   (given below), and match the tone the user requested. STICK
   TO WHAT THE USER ACTUALLY ASKED FOR: never invent extra
   claims, compliments, nicknames, or additional requests (e.g.
   asking for someone's phone number) that the user did not ask
   you to include. Elaborate on wording and tone, not on content.
5. Attachments: if the user mentions a file, or an attachment
   would clearly help, call list_files to see what exists, then
   ask_user whether to attach one. Never invent file paths.
6. save_draft is the DEFAULT action. Use send_email ONLY when the
   user explicitly asked to send. send_email itself will show
   the human the exact final text and require their typed
   confirmation before anything goes out — you do not need to
   pre-confirm through ask_user for this, just call send_email.
   send_email will also refuse and tell you if any placeholder
   text remains — fix it and call send_email again.
7. If required information is missing and is genuinely
   unknowable from context (see judgment call above), use
   ask_user instead of guessing.
8. If nothing matches the user's request, don't draft anything —
   say so in the report.
9. Use read_inbox when the user asks about received emails.
"""

# note: appended after the (possibly user-edited) core prompt. Built with
#       plain .format() and then brace-escaped, so it is NOT a template.
_SYSTEM_TAIL = """\
Sender identity: the person you're writing on behalf of is
{sender_name}. Sign emails with this real name — never with a
placeholder like "[Your Name]".
{draft_note}
After all tool calls are done, wrap your final report in this
format and provide no other text. This applies to EVERY turn of
the conversation — follow-up requests included — never reply with
plain prose or markdown instead of the report:
{format_instructions}"""

_NO_DRAFT_NOTE = """
NOTE: the user turned OFF "draft by default" in settings: when a
request implies the email should go out, you may call send_email
directly (it still shows the final text for human confirmation
unless that was disabled too).
"""


def _fallback_sender() -> str:
    return (
        "(not set — ask the user for their name once per session, "
        "then suggest adding SENDER_NAME to .env)"
    )


def build_agent(
    model: str | None = None,
    system_core: str | None = None,
    enabled_tools: list[str] | None = None,
    sender_name: str | None = None,
    draft_default: bool = True,
    verbose: bool = True,
) -> AgentExecutor:
    """Assemble a ready-to-run AgentExecutor from the current settings.
    Cheap to call — no network happens until .invoke()."""
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Please set OPENROUTER_API_KEY in your .env file.")

    sender = (sender_name if sender_name is not None else os.getenv("SENDER_NAME", "")).strip() or _fallback_sender()
    core = (system_core or DEFAULT_SYSTEM_PROMPT).strip()
    tail = _SYSTEM_TAIL.format(
        sender_name=sender,
        draft_note="" if draft_default else _NO_DRAFT_NOTE,
        format_instructions=parser.get_format_instructions(),
    )
    # note: the final system text is a LITERAL string. Escape every brace so
    #       ChatPromptTemplate never mistakes user-written {words} (or the
    #       JSON braces in format_instructions) for template variables.
    system_text = (core + "\n\n" + tail).replace("{", "{{").replace("}", "}}")

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_text),
            # note: chat_history lets a second request refer to the first one
            #       ("now send it") — the caller fills it per session.
            ("placeholder", "{chat_history}"),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )

    if enabled_tools is not None:
        tools = [t for t in ALL_TOOLS if t.name in set(enabled_tools)]
        if not tools:  # an agent with zero tools can't do anything useful
            tools = ALL_TOOLS
    else:
        tools = ALL_TOOLS

    # note: ChatOpenAI pointed at OpenRouter — swap the model via settings/.env.
    llm = ChatOpenAI(
        model=model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL),
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
    )

    agent = create_tool_calling_agent(llm=llm, prompt=prompt, tools=tools)
    # note: verbose=True prints every tool call so you can watch it think.
    return AgentExecutor(agent=agent, tools=tools, verbose=verbose)
