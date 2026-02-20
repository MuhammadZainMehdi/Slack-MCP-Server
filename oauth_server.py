"""
MCP Server with OAuth using FastAPI
Run: python oauth_server.py
"""

import os
import json
import asyncio
from slack_sdk import WebClient
from slack_sdk.oauth import AuthorizeUrlGenerator
from slack_sdk.oauth.state_store import FileOAuthStateStore
from fastapi import FastAPI, Request, Response, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi import APIRouter
from mcp.server.fastmcp import FastMCP
from pydantic import Field
import uvicorn
import threading
import tempfile
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Slack MCP Server")

CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get(
    "REDIRECT_URI", "http://localhost:3000/slack/oauth_redirect"
)
PORT = int(os.environ.get("PORT", 3000))
HOST = os.environ.get("HOST", "0.0.0.0")

print(f"CLIENT_ID loaded: {CLIENT_ID[:20] if CLIENT_ID else 'NOT SET'}...")

TOKEN_FILE = "tokens.json"


def load_tokens():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    return {}


def save_tokens(tokens):
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


def get_tokens_for_team(team_id):
    tokens = load_tokens()
    return tokens.get(team_id)


_current_bot_token = None
_current_user_token = None


def set_current_tokens(bot_token: str = None, user_token: str = None):
    global _current_bot_token, _current_user_token
    _current_bot_token = bot_token
    _current_user_token = user_token


def get_clients():
    global _current_bot_token, _current_user_token

    bot_token = _current_bot_token or os.environ.get("SLACK_BOT_TOKEN")
    user_token = _current_user_token or os.environ.get("SLACK_USER_TOKEN")

    if not bot_token:
        raise ValueError("No bot token. Install Slack app first.")
    if not user_token:
        raise ValueError("No user token. Install Slack app first.")

    return WebClient(token=user_token), WebClient(token=bot_token)


HTML_SUCCESS = """<!DOCTYPE html><html><head><title>Success</title></head><body style="font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f0f0f0;"><div style="background:white;padding:40px;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,0.1);text-align:center;"><h1 style="color:#2EB67D;">✓ Successfully Installed!</h1><p>Your Slack workspace is connected.</p></div></body></html>"""

HTML_ERROR = """<!DOCTYPE html><html><head><title>Error</title></head><body style="font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f0f0f0;"><div style="background:white;padding:40px;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,0.1);text-align:center;"><h1 style="color:#E01E5A;">✗ Installation Failed</h1><p>{error}</p></div></body></html>"""


@app.get("/slack/install")
def oauth_start():
    state_store = FileOAuthStateStore(
        expiration_seconds=600,
        base_dir=os.path.join(tempfile.gettempdir(), "mcp_states"),
    )
    state = state_store.issue()

    auth_url = AuthorizeUrlGenerator(
        client_id=CLIENT_ID,
        scopes=["channels:read", "groups:read", "chat:write"],
        user_scopes=["channels:history", "groups:history", "chat:write"],
        redirect_uri=REDIRECT_URI,
    )
    url = auth_url.generate(state=state)
    return RedirectResponse(url=url)


@app.get("/slack/oauth_redirect")
def oauth_callback(code: str = None, state: str = None, error: str = None):
    if error:
        return HTMLResponse(content=HTML_ERROR.format(error=error), status_code=400)

    if not code or not state:
        return HTMLResponse(
            content=HTML_ERROR.format(error="Missing parameters"), status_code=400
        )

    client = WebClient(token="")

    try:
        oauth_response = client.oauth_v2_access(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET, code=code
        )
    except Exception as e:
        return HTMLResponse(content=HTML_ERROR.format(error=str(e)), status_code=400)

    if not oauth_response.get("ok"):
        return HTMLResponse(
            content=HTML_ERROR.format(error=oauth_response.get("error", "Unknown")),
            status_code=400,
        )

    bot_token = oauth_response.get("access_token")
    user_token = oauth_response.get("authed_user", {}).get("access_token")
    team_id = oauth_response.get("team", {}).get("id")
    team_name = oauth_response.get("team", {}).get("name")
    bot_user_id = oauth_response.get("bot_user_id")

    tokens = load_tokens()
    tokens[team_id] = {
        "bot_token": bot_token,
        "user_token": user_token,
        "bot_user_id": bot_user_id,
        "team_id": team_id,
        "team_name": team_name,
    }
    save_tokens(tokens)

    return HTMLResponse(content=HTML_SUCCESS)


@app.get("/health")
def health():
    return {"status": "ok"}


def create_mcp():
    mcp = FastMCP("SlackMCP", log_level="ERROR")

    @mcp.tool(
        name="get_channel_messages", description="Fetch messages from a Slack channel"
    )
    def fetch_channel_messages(
        channel: str = Field(description="Channel name WITHOUT #"),
        limit: int = Field(default=50),
    ):
        from slack_sdk.errors import SlackApiError

        try:
            client, _ = get_clients()
            response = client.conversations_history(channel=channel, limit=limit)
            messages = response.get("messages", [])
            messages.reverse()
            return [msg.get("text", "") for msg in messages]
        except SlackApiError as e:
            return {"error": str(e)}

    @mcp.tool(name="list_channels", description="List all channels")
    def fetch_channels():
        try:
            client, _ = get_clients()
            response = client.conversations_list()
            return [
                {"id": c["id"], "name": c["name"]} for c in response.get("channels", [])
            ]
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool(name="post_message", description="Post a message to a channel")
    def send_message(
        channel: str = Field(description="Channel name WITHOUT #"),
        message: str = Field(description="Message text"),
    ):
        try:
            client, _ = get_clients()
            response = client.chat_postMessage(channel=channel, text=message)
            return {"ok": response.get("ok"), "ts": response.get("ts")}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool(name="get_threads", description="Fetch threads from a channel")
    def fetch_threads(
        channel: str = Field(description="Channel name WITHOUT #"),
        limit: int = Field(default=20),
    ):
        try:
            client, _ = get_clients()
            response = client.conversations_history(channel=channel, limit=limit)
            threads = []
            for msg in response.get("messages", []):
                if msg.get("reply_count", 0) > 0:
                    replies = client.conversations_replies(
                        channel=channel, ts=msg["ts"]
                    )
                    threads.append(
                        {
                            "parent_text": msg.get("text", ""),
                            "replies": [
                                r.get("text", "")
                                for r in replies.get("messages", [])[1:]
                            ],
                        }
                    )
            return threads
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool(name="reply_to_thread", description="Reply to a thread")
    def reply_thread(
        channel: str = Field(description="Channel name WITHOUT #"),
        thread_ts: str = Field(description="Thread timestamp"),
        message: str = Field(description="Reply message"),
    ):
        try:
            client, _ = get_clients()
            response = client.chat_postMessage(
                channel=channel, text=message, thread_ts=thread_ts
            )
            return {"ok": response.get("ok"), "ts": response.get("ts")}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool(name="search_messages", description="Search messages")
    def search_slack_messages(
        query: str = Field(description="Search query"), limit: int = Field(default=20)
    ):
        try:
            _, bot_client = get_clients()
            response = bot_client.search_messages(query=query, count=limit)
            return response.get("messages", {}).get("matches", [])
        except Exception as e:
            return {"error": str(e)}

    return mcp


mcp = create_mcp()


@app.post("/mcp")
async def mcp_handler(request: Request):
    team_id = request.headers.get("X-Team-ID")
    if not team_id:
        team_id = request.cookies.get("team_id")

    if team_id:
        team_tokens = get_tokens_for_team(team_id)
        if team_tokens:
            set_current_tokens(
                team_tokens.get("bot_token"), team_tokens.get("user_token")
            )

    body = await request.body()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    from starlette.datastructures import URL

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "query_string": request.query_string.encode(),
        "headers": [(k.encode(), v.encode()) for k, v in request.headers],
        "server": (HOST, PORT),
    }

    from mcp.server.streamable_http import StreamableHTTPServerTransport

    transport = StreamableHTTPServerTransport(mcp_session_id="slack-mcp")

    async def run():
        await transport.handle(scope, receive)
        return transport

    transport_result = await run()

    if hasattr(transport_result, "body") and transport_result.body:
        return Response(content=transport_result.body, media_type="application/json")

    return Response(status_code=202)


@app.get("/mcp")
def mcp_get():
    raise HTTPException(status_code=405, detail="Use POST")


if __name__ == "__main__":
    print(f"Starting server on http://{HOST}:{PORT}")
    print(f"OAuth install: http://{HOST}:{PORT}/slack/install")
    uvicorn.run(app, host=HOST, port=PORT)
