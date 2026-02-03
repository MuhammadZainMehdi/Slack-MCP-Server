# mcp_server.py

import os                                          # needed to read Railway's PORT env var
from contextlib import asynccontextmanager         # needed to wrap the ASGI lifespan
from fastapi import FastAPI                        # we use FastAPI as the HTTP layer
from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http import StreamableHTTPServerTransport
from pydantic import Field
from slack_tools import (
    get_channel_messages, 
    list_channels, 
    post_message, 
    get_threads,
    reply_to_thread as slack_reply_to_thread,
    search_messages,
    summarize_channel_source,
    extract_action_items
)

# ---------------------------------------------------------------------------
# CHANGE 1 — Initialize MCP server exactly the same way (no change here)
# ---------------------------------------------------------------------------
mcp = FastMCP("SlackMCP", log_level="ERROR")

# ---------------------------------------------------------------------------
# All your existing tools stay 100% unchanged below.
# Nothing inside @mcp.tool or @mcp.prompt needs to change for deployment.
# ---------------------------------------------------------------------------

@mcp.tool(
    name="get_channel_messages",
    description="Fetch the last messages from a Slack channel by name or ID"
)
def fetch_channel_messages(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    limit: int = Field(default=50, description="Number of messages to fetch (default 50)")
):
    return get_channel_messages(channel, limit)

@mcp.tool(
    name="list_channels",
    description="List all channels the bot has access to"
)
def fetch_channels():
    return list_channels()

@mcp.tool(
    name="post_message",
    description="Post a message to a Slack channel. Parameters: channel (string), message (string)"
)
def send_message(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    message: str = Field(description="The text message to post to the channel")
):
    return post_message(channel, message)

@mcp.tool(
    name="get_threads",
    description="Fetch the threads from a Slack channel by name or ID"
)
def fetch_threads(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    limit: int = Field(default=20, description="Number of threads to fetch (default 20)")
):
    return get_threads(channel, limit)

@mcp.tool(
    name="reply_to_thread",
    description="Reply to a thread in a Slack channel. Parameters: channel (string), thread_ts (string), message (string)"
)
def reply_thread(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    thread_ts: str = Field(description="The thread timestamp to reply to (e.g., '1768831010.322079')"),
    message: str = Field(description="The text message to post as a reply in the thread")
):
    return slack_reply_to_thread(channel, thread_ts, message)

@mcp.tool(
    name="search_messages",
    description="Search messages across Slack channels by text query"
)
def search_slack_messages(
    query: str = Field(description="Search query text (e.g., 'deployment failure')"),
    limit: int = Field(default=20, description="Maximum number of results")
):
    return search_messages(query, limit)

@mcp.tool(
    name="summarize_channel",
    description=(
        "Summarize recent messages from a Slack channel using LLM sampling. "
        "The server prepares the data; the client performs completion."
    )
)
def summarize_channel(
    channel: str = Field(description="Slack channel name (without #) or channel ID"),
    limit: int = Field(default=50, description="Number of recent messages to include in the summary")
):
    return summarize_channel_source(channel, limit)

@mcp.tool(
    name="extract_action_items",
    description="Extract actionable items from a Slack channel"
)
def extract_items_tool(
    channel: str,
    limit: int = Field(default=50, description="Number of messages to fetch (default 50)")
):
    messages = get_channel_messages(channel, limit)
    if not messages:
        return {"status": "empty", "message": "No messages to analyze"}
    items = extract_action_items(messages)
    return {"channel": channel, "action_items": items}

# ---------------------------------------------------------------------------
# Prompts — also unchanged
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="daily_channel_summary",
    description="Generate a daily summary of a Slack channel, focusing on important messages and decisions."
)
async def daily_channel_summary(channel: str, limit: int = 50):
    from slack_tools import summarize_channel_source
    payload = summarize_channel_source(channel, limit)
    if payload["sampled_text"]:
        prompt_text = payload["instructions"] + "\n\n" + "\n".join(payload["sampled_text"])
        return prompt_text
    else:
        return "No messages to summarize."

@mcp.prompt(
    name="action_items_summary",
    description="Extract actionable items from a Slack channel or thread."
)
async def action_items_summary(channel: str, limit: int = 50):
    from slack_tools import extract_action_items, get_channel_messages
    messages = get_channel_messages(channel, limit)
    if not messages:
        return "No messages in channel."
    items = extract_action_items(messages)
    if not items:
        return "No action items found."
    return items

@mcp.prompt(
    name="thread_followup",
    description="Summarize a Slack thread and optionally generate a reply using client LLM."
)
async def thread_followup(channel: str, thread_ts: str):
    from slack_tools import get_threads
    threads = get_threads(channel, limit=50)
    thread = next((t for t in threads if t["parent_ts"] == thread_ts), None)
    if not thread:
        return "Thread not found."
    messages = [thread["parent_text"]] + [r["text"] for r in thread.get("replies", [])]
    prompt_text = "Summarize the thread and optionally suggest a reply:\n\n" + "\n".join(messages)
    return prompt_text


# ---------------------------------------------------------------------------
# CHANGE 2 — The entire block below replaces the old `if __name__ == "__main__"`
#
# WHY: Railway is an HTTP platform. It exposes a PORT and expects your app
#      to listen on it. `mcp.run(transport="stdio")` only talks through
#      stdin/stdout — Railway can't reach that.
#
#      We mount the MCP server onto a FastAPI app using StreamableHTTP
#      transport, then run it with Uvicorn on the port Railway gives us.
#      A /health endpoint is added so Railway knows the service is alive.
# ---------------------------------------------------------------------------

# CHANGE 2a — Read the PORT Railway injects (defaults to 8000 for local dev)
PORT = int(os.environ.get("PORT", 8000))

# CHANGE 2b — Create a FastAPI app with a lifespan (startup/shutdown hook)
#             The lifespan does nothing heavy here, but FastAPI requires it
#             when you want clean startup/shutdown patterns.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup logic could go here later (e.g. DB connections) ---
    yield
    # --- shutdown logic could go here later ---

app = FastAPI(lifespan=lifespan)

# CHANGE 2c — Health-check endpoint.
#             Railway pings this to confirm your service started correctly.
#             Without it, Railway may keep restarting your container.
@app.get("/health")
async def health():
    return {"status": "ok"}

# CHANGE 2d — Mount the MCP server onto /mcp using StreamableHTTP transport.
#             This is the URL clients will hit to talk to your MCP tools.
#             e.g.  https://your-app.railway.app/mcp
mcp_transport = StreamableHTTPServerTransport(app=app, path="/mcp")
mcp.server = mcp_transport   # attach transport to your FastMCP instance


# CHANGE 2e — Entry-point: run with Uvicorn on the Railway-provided PORT.
#             0.0.0.0 binds to all interfaces (required for Railway/Docker).
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
