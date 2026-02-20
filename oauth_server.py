# oauth_server.py - Slack OAuth for MCP Server

import os
import uuid
import json
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import asyncio

from dotenv import load_dotenv

load_dotenv()

# OAuth Configuration
SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
REDIRECT_URI = os.environ.get("SLACK_REDIRECT_URI", "")

# Token storage (in production, use a database)
# Format: {team_id: {"user_token": ..., "bot_token": ..., "bot_user_id": ..., "team_id": ..., "installed_at": ...}}
token_store: Dict = {}

# Session storage for OAuth state
# Format: {state: {"team_id": ..., "redirect_uri": ..., "created_at": ...}}
session_store: Dict = {}


def generate_state(team_id: Optional[str] = None) -> str:
    """Generate a unique state parameter for OAuth flow."""
    state = str(uuid.uuid4())
    session_store[state] = {
        "team_id": team_id,
        "redirect_uri": REDIRECT_URI,
        "created_at": datetime.now(),
    }
    return state


def get_auth_url(state: str) -> str:
    """Generate Slack OAuth authorization URL."""
    # Bot token scopes - required for MCP server operations
    bot_scopes = [
        "channels:read",
        "channels:history",
        "channels:join",
        "chat:write",
        "groups:read",
        "groups:history",
        "im:read",
        "im:history",
        "im:write",
        "mpim:read",
        "mpim:history",
        "mpim:write",
        "search:read",
        "users:read",
        "users:read.email",
        "team:read",
        "reactions:read",
        "reactions:write",
    ]

    # User scopes - for identifying the user (simplified)
    user_scopes = [
        "users:read",
    ]

    url = (
        f"https://slack.com/oauth/v2/authorize?"
        f"client_id={SLACK_CLIENT_ID}&"
        f"scope={','.join(bot_scopes)}&"
        f"user_scope={','.join(user_scopes)}&"
        f"state={state}&"
        f"redirect_uri={REDIRECT_URI}"
    )
    return url


async def exchange_code_for_token(code: str, state: str) -> Optional[Dict]:
    """Exchange authorization code for access tokens."""
    if state not in session_store:
        raise ValueError("Invalid state parameter")

    session = session_store.pop(state)

    try:
        client = WebClient()

        # Use oauth.v2.access for user token + bot token
        response = client.oauth_v2_access(
            client_id=SLACK_CLIENT_ID,
            client_secret=SLACK_CLIENT_SECRET,
            code=code,
            redirect_uri=session["redirect_uri"],
        )

        # Extract response as dictionary - handle different response types
        raw_result: Dict = {}

        # Method 1: Check if response has 'data' attribute with dict
        if hasattr(response, "data") and isinstance(response.data, dict):
            raw_result = response.data
        # Method 2: Check if response itself is a dict-like object
        elif hasattr(response, "get") and hasattr(response, "keys"):
            raw_result = {
                k: response.get(k) for k in dir(response) if not k.startswith("_")
            }

        # Convert any bytes values to strings
        def convert_bytes(val):
            if isinstance(val, bytes):
                return val.decode("utf-8")
            elif isinstance(val, dict):
                return {str(k): convert_bytes(v) for k, v in val.items()}
            elif isinstance(val, list):
                return [convert_bytes(v) for v in val]
            return val

        auth_result: Dict[str, Any] = convert_bytes(raw_result)  # type: ignore

        if not auth_result.get("ok", True) and not auth_result.get("access_token"):
            error_msg = auth_result.get("error", "Unknown error")
            raise ValueError(f"Slack OAuth failed: {error_msg}")

        # Extract tokens - handle both string and bytes
        team_info = auth_result.get("team", {}) or {}
        authed_user = auth_result.get("authed_user", {}) or {}

        team_id = str(team_info.get("id")) if team_info.get("id") else None
        user_token = (
            str(authed_user.get("access_token"))
            if authed_user.get("access_token")
            else None
        )
        bot_token = (
            str(auth_result.get("access_token"))
            if auth_result.get("access_token")
            else None
        )
        bot_user_id = (
            str(auth_result.get("bot_user_id"))
            if auth_result.get("bot_user_id")
            else None
        )

        if not user_token:
            raise ValueError("No user token received")

        if not bot_token:
            raise ValueError("No bot token received")

        if not team_id:
            raise ValueError("No team ID received")

        # Store tokens
        token_store[team_id] = {
            "user_token": user_token,
            "bot_token": bot_token,
            "bot_user_id": bot_user_id,
            "team_id": team_id,
            "team_name": str(team_info.get("name")) if team_info.get("name") else None,
            "installed_at": datetime.now().isoformat(),
            "installer_user_id": str(authed_user.get("id"))
            if authed_user.get("id")
            else None,
        }

        # Auto-install bot to workspace
        await install_bot_to_workspace(bot_token, bot_user_id, team_id)

        return {
            "team_id": team_id,
            "team_name": str(team_info.get("name")) if team_info.get("name") else None,
            "bot_user_id": bot_user_id,
            "installer_user_id": str(authed_user.get("id"))
            if authed_user.get("id")
            else None,
        }

    except SlackApiError as e:
        error_msg = (
            e.response.get("error", str(e)) if hasattr(e, "response") else str(e)
        )
        raise ValueError(f"Slack API error: {error_msg}")
    except Exception as e:
        raise ValueError(f"OAuth error: {str(e)}")


async def install_bot_to_workspace(
    bot_token: str, bot_user_id: Optional[str], team_id: str
):
    """Auto-install bot to workspace and join common channels."""
    try:
        bot_client = WebClient(token=bot_token)

        # Get list of public channels
        try:
            channels_response = bot_client.conversations_list(
                types="public_channel", limit=100
            )

            if channels_response.get("ok"):
                channels = channels_response.get("channels", [])

                # Auto-join public channels
                for channel in channels:
                    channel_id = channel.get("id")
                    if channel_id:
                        try:
                            bot_client.conversations_join(channel=channel_id)
                        except SlackApiError:
                            pass  # Skip if can't join (private channel, etc.)

        except SlackApiError:
            pass  # Continue even if can't list channels

        return {"status": "success", "bot_user_id": bot_user_id}

    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_token_for_team(team_id: str) -> Optional[Dict]:
    """Get stored tokens for a team."""
    return token_store.get(team_id)


def get_all_teams() -> Dict[str, Dict]:
    """Get all registered teams."""
    return token_store.copy()


def remove_team(team_id: str):
    """Remove team tokens (for uninstall)."""
    if team_id in token_store:
        del token_store[team_id]


def validate_token(token: str) -> Optional[str]:
    """Validate a token and return the team_id."""
    try:
        client = WebClient(token=token)
        auth_response = client.auth_test()

        if auth_response.get("ok"):
            return auth_response.get("team_id")
    except Exception:
        pass
    return None


async def refresh_token(team_id: str) -> bool:
    """Refresh tokens for a team (if needed)."""
    # Note: Slack user tokens don't expire, but bot tokens do
    # This is a placeholder for token refresh logic if needed
    return True


def get_user_info(user_token: str, user_id: str) -> Optional[Dict]:
    """Get user information using user token."""
    try:
        client = WebClient(token=user_token)
        response = client.users_info(user=user_id)
        if response.get("ok"):
            user = response.get("user", {})
            return {
                "id": user.get("id"),
                "name": user.get("name"),
                "real_name": user.get("real_name"),
                "email": user.get("profile", {}).get("email"),
                "avatar": user.get("profile", {}).get("image_72"),
            }
    except Exception:
        pass
    return None
