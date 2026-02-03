# slack_tools.py

import os
import re
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv          # still here — harmless on Railway (does nothing if no .env)
from typing import List, Dict

load_dotenv()                           # works locally; on Railway env vars are injected by the platform

# ---------------------------------------------------------------------------
# CHANGE 1 — Remove the old top-level client creation block entirely.
#
# OLD CODE (deleted):
#     slack_token = os.environ.get("SLACK_USER_TOKEN")
#     slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
#     if not slack_token:
#         raise ValueError("SLACK_USER_TOKEN environment variable not set")
#     client = WebClient(token=slack_token)
#     bot_client = WebClient(token=slack_bot_token)
#
# WHY: Python runs top-level code the moment the file is imported.
#      On Railway the app boots up, imports this file, and immediately
#      tries to read the env vars.  If Railway hasn't finished injecting
#      them yet (or if you haven't set them in the Railway dashboard yet),
#      the server crashes before it even starts.
#
#      The fix: create the clients inside a helper function that only runs
#      when a tool is actually called.  By that point Railway has fully
#      started and the env vars are guaranteed to be available.
# ---------------------------------------------------------------------------

def _get_clients():
    """
    Lazy factory — returns (user_client, bot_client) every time it is called.
    Reads env vars at call-time, not at import-time.
    """
    user_token = os.environ.get("SLACK_USER_TOKEN")
    bot_token  = os.environ.get("SLACK_BOT_TOKEN")

    if not user_token:
        raise ValueError("SLACK_USER_TOKEN environment variable not set")
    if not bot_token:
        raise ValueError("SLACK_BOT_TOKEN environment variable not set")

    return WebClient(token=user_token), WebClient(token=bot_token)


# ---------------------------------------------------------------------------
# CHANGE 2 — Every function that used the old `client` / `bot_client`
#            globals now calls `_get_clients()` at the top of the function.
#            The rest of each function's logic is UNCHANGED.
# ---------------------------------------------------------------------------

def list_channels(types: str = "public_channel,private_channel") -> List[Dict]:
    client, _ = _get_clients()                  # ← only change in this function
    try:
        response = client.conversations_list(types=types)
        channels = response.get("channels", [])
        return [{"id": c["id"], "name": c["name"]} for c in channels]
    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to list channels: {str(e)}")


def get_channel_id(channel: str) -> str:
    # No direct Slack call here, but it calls list_channels() which handles its own client
    if channel.startswith("#"):
        channel_name = channel[1:]
    else:
        channel_name = channel

    if channel.startswith(("C", "G", "D")):
        return channel

    channels = list_channels()
    for c in channels:
        if c["name"] == channel_name:
            return c["id"]

    raise ValueError(f"Channel '{channel}' not found or bot is not a member.")


def join_channel_if_needed(channel_id: str):
    client, _ = _get_clients()                  # ← only change
    try:
        client.conversations_join(channel=channel_id)
    except SlackApiError as e:
        if e.response["error"] == "method_not_supported_for_channel_type":
            pass
        elif e.response["error"] == "already_in_channel":
            pass
        else:
            raise


def get_channel_messages(channel: str, limit: int = 50) -> List[str]:
    client, _ = _get_clients()                  # ← only change
    try:
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        response = client.conversations_history(channel=channel_id, limit=limit)
        messages = response.get("messages", [])
        messages.reverse()
        return [msg.get("text", "") for msg in messages]
    except SlackApiError as e:
        if e.response["error"] == "not_in_channel":
            raise ValueError(
                f"Bot is not a member of channel '{channel}'. Please invite the bot to the channel."
            )
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to fetch messages: {str(e)}")


def post_message(channel: str, message: str) -> Dict:
    client, _ = _get_clients()                  # ← only change
    try:
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        response = client.chat_postMessage(channel=channel_id, text=message)
        return {
            "channel": channel,
            "ts": response.get("ts"),
            "message": message,
            "ok": response.get("ok", False)
        }
    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to post message: {str(e)}")


def get_threads(channel: str, limit: int = 20) -> List[Dict]:
    client, _ = _get_clients()                  # ← only change
    try:
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        history = client.conversations_history(channel=channel_id, limit=limit)
        threads = []

        for msg in history.get("messages", []):
            if msg.get("reply_count", 0) > 0 and "ts" in msg:
                replies_resp = client.conversations_replies(channel=channel_id, ts=msg["ts"])
                replies = replies_resp.get("messages", [])
                threads.append({
                    "thread_ts": msg["ts"],
                    "parent_text": msg.get("text", ""),
                    "reply_count": msg.get("reply_count", 0),
                    "replies": [
                        {"user": r.get("user"), "text": r.get("text"), "ts": r.get("ts")}
                        for r in replies[1:]
                    ]
                })
        return threads
    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to fetch threads: {str(e)}")


def reply_to_thread(channel: str, thread_ts: str, message: str) -> Dict:
    client, _ = _get_clients()                  # ← only change
    try:
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        response = client.chat_postMessage(channel=channel_id, text=message, thread_ts=thread_ts)
        return {
            "channel": channel,
            "thread_ts": thread_ts,
            "ts": response.get("ts"),
            "message": message,
            "ok": response.get("ok", False)
        }
    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to reply to thread: {str(e)}")


def search_messages(query: str, limit: int = 20) -> List[Dict]:
    _, bot_client = _get_clients()              # ← only change (uses bot_client)
    try:
        response = bot_client.search_messages(query=query, count=limit)
        matches = response.get("messages", {}).get("matches", [])
        results = []
        for msg in matches:
            results.append({
                "channel": msg.get("channel", {}).get("name"),
                "channel_id": msg.get("channel", {}).get("id"),
                "user": msg.get("username") or msg.get("user"),
                "text": msg.get("text"),
                "ts": msg.get("ts"),
                "permalink": msg.get("permalink")
            })
        return results
    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to search messages: {str(e)}")


def summarize_channel_source(channel: str, limit: int = 50):
    messages = get_channel_messages(channel, limit)
    if not messages:
        return {"instructions": "No messages found in the channel.", "sampled_text": []}

    sampled_text = []
    for msg in messages:
        if isinstance(msg, str):
            text = msg.strip()
            if text:
                sampled_text.append(text)
        elif isinstance(msg, dict):
            user = msg.get("user", "unknown")
            text = msg.get("text", "").strip()
            if text:
                sampled_text.append(f"{user}: {text}")

    return {
        "instructions": (
            "Summarize the following Slack channel conversation.\n"
            "Focus on:\n"
            "- Key topics discussed\n"
            "- Important updates or decisions\n"
            "- Actionable takeaways (if any)\n\n"
            "Keep the summary concise and structured."
        ),
        "sampled_text": sampled_text
    }


def extract_action_items(messages: List[str]) -> List[str]:
    action_verbs = [
        "assign", "complete", "review", "update", "follow up",
        "schedule", "submit", "plan", "fix", "check", "resolve"
    ]
    action_items = []
    for msg in messages:
        msg_lower = msg.lower()
        if any(verb in msg_lower for verb in action_verbs):
            clean_msg = re.sub(r"<@[\w]+>", "", msg).strip()
            action_items.append(clean_msg)
    return action_items