"""
main.py — the terminal session for the general-purpose email agent.

The agent can handle any email task: writing, replying, announcements,
invitations, reminders, batch emails to a filtered group from the
contacts sheet, attachments, and reading your inbox.

Design decisions (see tools.py for the tools, agent_core.py for the brain):
  - drafts by default; real sending only when you say "send" AND confirm
  - the agent asks YOU questions mid-run (ask_user) when info is missing
    or to offer an attachment
  - facts about your business come from the knowledge/ folder (light RAG),
    not from the model's imagination

Prefer a UI? `python server.py` serves the same agent at
http://localhost:8000 (the AgentDock frontend).
"""

import sys

# note: Windows terminals default to an old encoding (cp1252) that
#       crashes on emoji/foreign characters in verbose logs — this fixes it.
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage

load_dotenv()  # Load environment variables from .env file — BEFORE building the agent

from agent_core import build_agent, parser


def print_report(raw_output: str) -> None:
    """note: parse the model's JSON answer into the EmailReport object and
    print it nicely; if parsing fails, show the raw text instead."""
    try:
        r = parser.parse(raw_output)
        print("\n=== REPORT ===")
        print(f"Task:        {r.task}")
        print(f"Recipients:  {', '.join(r.recipients) if r.recipients else 'none'}")
        print(f"Action:      {r.action}")
        print(f"Subject:     {r.subject or '-'}")
        print(f"Attachments: {', '.join(r.attachments) if r.attachments else 'none'}")
        print(f"Summary:     {r.summary}")
        print(f"Tools used:  {', '.join(r.tools_used)}")
    except Exception as e:
        print("Could not parse structured output:", e)
        print("Raw output was:", raw_output)


if __name__ == "__main__":
    agent_executor = build_agent()

    # note: chat_history keeps the conversation alive across requests in
    #       this session, so "now send it" understands what "it" is.
    chat_history = []
    print("Email agent ready. Type your request, or 'exit' to quit.")
    while True:
        try:
            query = input("\nWhat should the email agent do? ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query or query.lower() in {"exit", "quit"}:
            break

        try:
            raw_response = agent_executor.invoke(
                {"input": query, "chat_history": chat_history}
            )
        except Exception as e:
            # note: one failed request must not kill the whole session.
            print(f"Agent error ({type(e).__name__}): {e}")
            continue

        print_report(raw_response["output"])
        chat_history.append(HumanMessage(content=query))
        chat_history.append(AIMessage(content=raw_response["output"]))
