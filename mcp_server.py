# mcp_server.py - Slack MCP Server with OAuth

import os
import uvicorn
import asyncio
from contextvars import ContextVar
from fastapi import FastAPI, Request, Query
from fastapi.responses import RedirectResponse, JSONResponse
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from typing import Optional

from slack_tools import (
    get_channel_messages,
    list_channels,
    post_message,
    get_threads,
    reply_to_thread as slack_reply_to_thread,
    search_messages,
    summarize_channel_source,
    extract_action_items,
)

from oauth_server import (
    generate_state,
    get_auth_url,
    exchange_code_for_token,
    get_token_for_team,
    get_all_teams,
    token_store,
    session_store,
)

mcp = FastMCP("SlackMCP", log_level="ERROR")

# Token context variable for request-scoped token
request_token_var: ContextVar[Optional[str]] = ContextVar("request_token", default=None)
request_team_var: ContextVar[Optional[str]] = ContextVar("request_team", default=None)


def get_user_token() -> Optional[str]:
    """Get the user token for the current request."""
    return request_token_var.get()


def get_team_id() -> Optional[str]:
    """Get the team ID for the current request."""
    return request_team_var.get()


mcp_app = mcp.streamable_http_app()


class TokenMiddleware:
    """ASGI middleware to extract authorization token and team from request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")

            # Skip OAuth endpoints
            if path.startswith("/oauth") or path.startswith("/auth"):
                await self.app(scope, receive, send)
                return

            headers = dict(scope.get("headers", {}))
            auth_header = headers.get(b"authorization", b"").decode("utf-8")

            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
            elif auth_header:
                token = auth_header
            else:
                token = None

            # Look up team from token if provided
            team_id = None
            if token:
                for tid, data in token_store.items():
                    if (
                        data.get("user_token") == token
                        or data.get("bot_token") == token
                    ):
                        team_id = tid
                        break

            request_token_var.set(token)
            request_team_var.set(team_id)

        await self.app(scope, receive, send)


# Create FastAPI app for OAuth endpoints
api = FastAPI(title="Slack MCP OAuth")


# OAuth Routes
@api.get("/auth/login")
async def login():
    """Start OAuth flow - redirects to Slack authorization."""
    state = generate_state()
    auth_url = get_auth_url(state)
    return RedirectResponse(url=auth_url)


@api.get("/auth/callback")
async def oauth_callback(code: str = Query(...), state: str = Query(...)):
    """OAuth callback - exchange code for tokens."""
    try:
        result = await exchange_code_for_token(code, state)
        return JSONResponse(
            {
                "success": True,
                "message": "Successfully authenticated with Slack",
                "team_id": result.get("team_id"),
                "team_name": result.get("team_name"),
            }
        )
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@api.get("/auth/status")
async def auth_status():
    """Check authentication status - list connected teams."""
    teams = get_all_teams()
    return {
        "teams": [
            {
                "team_id": tid,
                "team_name": data.get("team_name"),
                "installed_at": data.get("installed_at"),
            }
            for tid, data in teams.items()
        ]
    }


@api.get("/auth/install/{team_id}")
async def get_install_url(team_id: str):
    """Get OAuth install URL for a specific team."""
    state = generate_state(team_id)
    auth_url = get_auth_url(state)
    return {"url": auth_url}


# Mount MCP at /mcp
app = TokenMiddleware(mcp_app)
api.mount("/mcp", app)


# MCP Tools
@mcp.tool(
    name="get_channel_messages",
    description="Fetch the last messages from a Slack channel by name or ID",
)
def fetch_channel_messages(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    limit: int = Field(
        default=50, description="Number of messages to fetch (default 50)"
    ),
):
    user_token = get_user_token()
    team_id = get_team_id()

    # Use team-specific token if available
    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return get_channel_messages(channel, limit, user_token)


@mcp.tool(name="list_channels", description="List all channels the bot has access to")
def fetch_channels():
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return list_channels(user_token=user_token)


@mcp.tool(
    name="post_message",
    description="Post a message to a Slack channel. Parameters: channel (string), message (string)",
)
def send_message(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    message: str = Field(description="The text message to post to the channel"),
):
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return post_message(channel, message, user_token)


@mcp.tool(
    name="get_threads",
    description="Fetch the threads from a Slack channel by name or ID",
)
def fetch_threads(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    limit: int = Field(
        default=20, description="Number of threads to fetch (default 20)"
    ),
):
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return get_threads(channel, limit, user_token)


@mcp.tool(
    name="reply_to_thread",
    description="Reply to a thread in a Slack channel. Parameters: channel (string), thread_ts (string), message (string)",
)
def reply_thread(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    thread_ts: str = Field(
        description="The thread timestamp to reply to (e.g., '1768831010.322079')"
    ),
    message: str = Field(
        description="The text message to post as a reply in the thread"
    ),
):
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return slack_reply_to_thread(channel, thread_ts, message, user_token)


@mcp.tool(
    name="search_messages",
    description="Search messages across Slack channels by text query",
)
def search_slack_messages(
    query: str = Field(description="Search query text (e.g., 'deployment failure')"),
    limit: int = Field(default=20, description="Maximum number of results"),
):
    return search_messages(query, limit)


@mcp.tool(
    name="summarize_channel",
    description=(
        "Summarize recent messages from a Slack channel using LLM sampling. "
        "The server prepares the data; the client performs completion."
    ),
)
def summarize_channel(
    channel: str = Field(description="Slack channel name (without #) or channel ID"),
    limit: int = Field(
        default=50, description="Number of recent messages to include in the summary"
    ),
):
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return summarize_channel_source(channel, limit, user_token)


@mcp.tool(
    name="extract_action_items",
    description="Extract actionable items from a Slack channel",
)
def extract_items_tool(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    limit: int = Field(
        default=50, description="Number of messages to fetch (default 50)"
    ),
):
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    messages = get_channel_messages(channel, limit, user_token)
    if not messages:
        return {"status": "empty", "message": "No messages to analyze"}
    items = extract_action_items(messages)
    return {"channel": channel, "action_items": items}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(api, host="0.0.0.0", port=port)
