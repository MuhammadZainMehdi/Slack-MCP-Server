# mcp_server.py - Slack MCP Server with OAuth

import os
import json
import uvicorn
from contextvars import ContextVar
from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response
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

# Token context variable for request-scoped token
request_token_var: ContextVar[Optional[str]] = ContextVar("request_token", default=None)
request_team_var: ContextVar[Optional[str]] = ContextVar("request_team", default=None)


def get_user_token() -> Optional[str]:
    return request_token_var.get()


def get_team_id() -> Optional[str]:
    return request_team_var.get()


# Create MCP server
mcp = FastMCP("SlackMCP", log_level="ERROR")


# Define MCP tools
@mcp.tool(
    name="get_channel_messages", description="Fetch messages from a Slack channel"
)
def fetch_channel_messages(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    limit: int = Field(default=50, description="Number of messages"),
):
    user_token = get_user_token()
    team_id = get_team_id()
    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")
    return get_channel_messages(channel, limit, user_token)


@mcp.tool(name="list_channels", description="List all Slack channels")
def fetch_channels():
    user_token = get_user_token()
    team_id = get_team_id()
    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")
    return list_channels(user_token=user_token)


@mcp.tool(name="post_message", description="Post a message to Slack")
def send_message(
    channel: str = Field(description="Channel name"),
    message: str = Field(description="Message to post"),
):
    user_token = get_user_token()
    team_id = get_team_id()
    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")
    return post_message(channel, message, user_token)


@mcp.tool(name="get_threads", description="Get threads from a channel")
def fetch_threads(
    channel: str = Field(description="Channel name"),
    limit: int = Field(default=20, description="Number of threads"),
):
    user_token = get_user_token()
    team_id = get_team_id()
    if team_id and not user_token:
        team_data = get_token_for_team(team_id)
        if team_data:
            user_token = team_data.get("user_token")
    return get_threads(channel, limit, user_token)


@mcp.tool(name="reply_to_thread", description="Reply to a thread")
def reply_thread(
    channel: str = Field(description="Channel name"),
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


@mcp.tool(name="search_messages", description="Search Slack messages")
def search_slack_messages(
    query: str = Field(description="Search query"),
    limit: int = Field(default=20, description="Max results"),
):
    return search_messages(query, limit)


@mcp.tool(name="summarize_channel", description="Summarize channel messages")
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


@mcp.tool(name="extract_action_items", description="Extract action items")
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


# Create MCP HTTP app
mcp_app = mcp.streamable_http_app()


# Create FastAPI for OAuth endpoints
api = FastAPI(title="SlackMCP OAuth")


# FastAPI routes for OAuth - at /auth prefix
@api.get("/")
async def root():
    return {"status": "ok", "message": "Slack MCP Server - OAuth at /auth"}


@api.get("/health")
async def health():
    return {"status": "healthy"}


@api.get("/login")
async def login():
    state = generate_state()
    auth_url = get_auth_url(state)
    return RedirectResponse(url=auth_url)


@api.get("/callback")
async def oauth_callback(code: str = Query(...), state: str = Query(...)):
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


@api.get("/status")
async def auth_status():
    teams = get_all_teams()
    return {
        "teams": [
            {"team_id": t, "team_name": d.get("team_name")} for t, d in teams.items()
        ]
    }


# Static metadata endpoints
@api.get("/.well-known/mcp/server-card.json")
async def server_card():
    card_path = os.path.join(os.path.dirname(__file__), "server-card.json")
    if os.path.exists(card_path):
        with open(card_path, "r") as f:
            return JSONResponse(content=json.load(f))
    return JSONResponse({"error": "Not found"}, status_code=404)


@api.get("/.well-known/oauth-protected-resource")
async def oauth_resource():
    return JSONResponse(
        {
            "resource": "https://slack-mcp.up.railway.app",
            "authorization_servers": ["https://auth.smithery.ai"],
            "scopes_supported": [
                "channels:read",
                "channels:history",
                "chat:write",
                "groups:read",
                "search:read",
                "users:read",
            ],
        }
    )


# Mount FastAPI at /auth and MCP at root
# Using a custom route to handle this
from fastapi import FastAPI
from starlette.routing import Route, Mount
from starlette.responses import Response


async def mcp_handler(request: Request) -> Response:
    """Handle MCP requests."""
    # Extract auth header for token
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        # Find team from token
        for tid, data in token_store.items():
            if data.get("user_token") == token or data.get("bot_token") == token:
                request_team_var.set(tid)
                break
        request_token_var.set(token)

    # Get response from MCP app
    # MCP app is ASGI - we need to handle it differently
    # Use the mcp app directly
    return await mcp_app(request.scope, request.receive, request._send)


# Simpler approach: Use FastAPI with MCP mounted
# Create main app
main_app = FastAPI()


# Add MCP at root - catch all routes
@main_app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def handle_all(path: str, request: Request):
    """Handle MCP at root, except for /auth which goes to FastAPI."""
    if path.startswith("auth/"):
        # Let FastAPI handle auth routes
        return await api(request.scope, request.receive, request._send)

    # For MCP requests, extract token and handle
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        for tid, data in token_store.items():
            if data.get("user_token") == token or data.get("bot_token") == token:
                request_team_var.set(tid)
                break
        request_token_var.set(token)

    return await mcp_app(request.scope, request.receive, request._send)


# Also handle root path
@main_app.get("/")
@main_app.post("/")
async def root_handler(request: Request):
    return await mcp_app(request.scope, request.receive, request._send)


# Add health and static endpoints directly
@main_app.get("/health")
async def health():
    return {"status": "healthy"}


@main_app.get("/.well-known/mcp/server-card.json")
async def server_card():
    card_path = os.path.join(os.path.dirname(__file__), "server-card.json")
    if os.path.exists(card_path):
        with open(card_path, "r") as f:
            return JSONResponse(content=json.load(f))
    return JSONResponse({"error": "Not found"}, status_code=404)


@main_app.get("/.well-known/oauth-protected-resource")
async def oauth_resource():
    return JSONResponse(
        {
            "resource": "https://slack-mcp.up.railway.app",
            "authorization_servers": ["https://auth.smithery.ai"],
            "scopes_supported": [
                "channels:read",
                "channels:history",
                "chat:write",
                "groups:read",
                "search:read",
                "users:read",
            ],
        }
    )


# Also mount auth routes
main_app.mount("/auth", api)


app = main_app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
