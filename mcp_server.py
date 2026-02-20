# mcp_server.py - Slack MCP Server with OAuth

import os
import uvicorn
from contextvars import ContextVar
from fastapi import FastAPI, Query
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
)

# Create FastAPI first - this is the main app
api = FastAPI(title="Slack MCP OAuth")

# Token context variable for request-scoped token
request_token_var: ContextVar[Optional[str]] = ContextVar("request_token", default=None)
request_team_var: ContextVar[Optional[str]] = ContextVar("request_team", default=None)


def get_user_token() -> Optional[str]:
    return request_token_var.get()


def get_team_id() -> Optional[str]:
    return request_team_var.get()


# Create MCP server
mcp = FastMCP("SlackMCP", log_level="ERROR")


# OAuth Routes
@api.get("/")
async def root():
    return {"status": "ok", "message": "Slack MCP Server with OAuth"}


@api.get("/health")
async def health():
    return {"status": "healthy"}


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


import json
import os


# Serve server-card.json for Smithery scanning
@api.get("/.well-known/mcp/server-card.json")
async def server_card():
    """Return server metadata for Smithery."""
    card_path = os.path.join(os.path.dirname(__file__), "server-card.json")
    if os.path.exists(card_path):
        with open(card_path, "r") as f:
            return JSONResponse(content=json.load(f))
    return JSONResponse(content={"error": "Server card not found"}, status_code=404)


# Token Middleware for MCP requests
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.middleware import Middleware


async def http_middleware(request, call_next):
    """Extract token from Authorization header."""
    auth_header = request.headers.get("authorization", "")

    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    elif auth_header:
        token = auth_header
    else:
        token = None

    # Look up team from token
    team_id = None
    if token:
        for tid, data in token_store.items():
            if data.get("user_token") == token or data.get("bot_token") == token:
                team_id = tid
                break

    request_token_var.set(token)
    request_team_var.set(team_id)

    return await call_next(request)


# Create MCP app
mcp_app = mcp.streamable_http_app()


# Mount MCP at /mcp
api.mount("/mcp", mcp_app)


# MCP Tools - using tokens from context
@mcp.tool(
    name="get_channel_messages",
    description="Fetch the last messages from a Slack channel",
)
def fetch_channel_messages(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    limit: int = Field(default=50, description="Number of messages to fetch"),
):
    user_token = get_user_token()
    team_id = get_team_id()

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


@mcp.tool(name="post_message", description="Post a message to a Slack channel")
def send_message(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    message: str = Field(description="The text message to post"),
):
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return post_message(channel, message, user_token)


@mcp.tool(name="get_threads", description="Fetch threads from a Slack channel")
def fetch_threads(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    limit: int = Field(default=20, description="Number of threads to fetch"),
):
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return get_threads(channel, limit, user_token)


@mcp.tool(name="reply_to_thread", description="Reply to a thread in a Slack channel")
def reply_thread(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    thread_ts: str = Field(description="Thread timestamp"),
    message: str = Field(description="Reply message"),
):
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return slack_reply_to_thread(channel, thread_ts, message, user_token)


@mcp.tool(name="search_messages", description="Search messages across Slack")
def search_slack_messages(
    query: str = Field(description="Search query"),
    limit: int = Field(default=20, description="Max results"),
):
    return search_messages(query, limit)


@mcp.tool(name="summarize_channel", description="Summarize Slack channel messages")
def summarize_channel(
    channel: str = Field(description="Channel name"),
    limit: int = Field(default=50, description="Number of messages"),
):
    user_token = get_user_token()
    team_id = get_team_id()

    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")

    return summarize_channel_source(channel, limit, user_token)


@mcp.tool(name="extract_action_items", description="Extract action items from channel")
def extract_items_tool(
    channel: str = Field(description="Channel name"),
    limit: int = Field(default=50, description="Number of messages"),
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

app = api
