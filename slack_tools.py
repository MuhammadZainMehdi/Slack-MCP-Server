import os
import json
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
from typing import List, Dict, Optional
import re

load_dotenv()

_current_bot_token = None
_current_user_token = None


def set_current_tokens(bot_token: str, user_token: Optional[str] = None):
    global _current_bot_token, _current_user_token
    _current_bot_token = bot_token
    _current_user_token = user_token


def get_clients():
    global _current_bot_token, _current_user_token

    bot_token = _current_bot_token or os.environ.get("SLACK_BOT_TOKEN")
    user_token = _current_user_token or os.environ.get("SLACK_USER_TOKEN")

    if not bot_token:
        raise ValueError("No bot token available. Please install the Slack app first.")
    if not user_token:
        raise ValueError("No user token available. Please install the Slack app first.")

    return WebClient(token=user_token), WebClient(token=bot_token)


def list_channels(types: str = "public_channel,private_channel") -> List[Dict]:
    try:
        client, _ = get_clients()
        response = client.conversations_list(types=types)
        channels = response.get("channels", [])
        return [{"id": c["id"], "name": c["name"]} for c in channels]

    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to list channels: {str(e)}")


def get_channel_id(channel: str) -> str:
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
    try:
        client, _ = get_clients()
        client.conversations_join(channel=channel_id)
    except SlackApiError as e:
        if e.response["error"] == "method_not_supported_for_channel_type":
            pass
        elif e.response["error"] == "already_in_channel":
            pass
        else:
            raise


def get_channel_messages(channel: str, limit: int = 50) -> List[str]:
    try:
        client, _ = get_clients()
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
    try:
        client, _ = get_clients()
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        response = client.chat_postMessage(channel=channel_id, text=message)

        return {
            "channel": channel,
            "ts": response.get("ts"),
            "message": message,
            "ok": response.get("ok", False),
        }

    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to post message: {str(e)}")


def get_threads(channel: str, limit: int = 20) -> List[Dict]:
    try:
        client, _ = get_clients()
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        history = client.conversations_history(channel=channel_id, limit=limit)

        threads = []

        for msg in history.get("messages", []):
            if msg.get("reply_count", 0) > 0 and "ts" in msg:
                replies_resp = client.conversations_replies(
                    channel=channel_id, ts=msg["ts"]
                )

                replies = replies_resp.get("messages", [])

                threads.append(
                    {
                        "thread_ts": msg["ts"],
                        "parent_text": msg.get("text", ""),
                        "reply_count": msg.get("reply_count", 0),
                        "replies": [
                            {
                                "user": r.get("user"),
                                "text": r.get("text"),
                                "ts": r.get("ts"),
                            }
                            for r in replies[1:]
                        ],
                    }
                )

        return threads

    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to fetch threads: {str(e)}")


def reply_to_thread(channel: str, thread_ts: str, message: str) -> Dict:
    try:
        client, _ = get_clients()
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        response = client.chat_postMessage(
            channel=channel_id, text=message, thread_ts=thread_ts
        )

        return {
            "channel": channel,
            "thread_ts": thread_ts,
            "ts": response.get("ts"),
            "message": message,
            "ok": response.get("ok", False),
        }

    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to reply to thread: {str(e)}")


def search_messages(query: str, limit: int = 20) -> List[Dict]:
    try:
        _, bot_client = get_clients()

        response = bot_client.search_messages(query=query, count=limit)

        matches = response.get("messages", {}).get("matches", [])

        results = []
        for msg in matches:
            results.append(
                {
                    "channel": msg.get("channel", {}).get("name"),
                    "channel_id": msg.get("channel", {}).get("id"),
                    "user": msg.get("username") or msg.get("user"),
                    "text": msg.get("text"),
                    "ts": msg.get("ts"),
                    "permalink": msg.get("permalink"),
                }
            )

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
        "sampled_text": sampled_text,
    }


def extract_action_items(messages: List[str]) -> List[str]:
    action_verbs = [
        "assign",
        "complete",
        "review",
        "update",
        "follow up",
        "schedule",
        "submit",
        "plan",
        "fix",
        "check",
        "resolve",
    ]

    action_items = []

    for msg in messages:
        msg_lower = msg.lower()
        if any(verb in msg_lower for verb in action_verbs):
            clean_msg = re.sub(r"<@[\w]+>", "", msg).strip()
            action_items.append(clean_msg)

    return action_items
