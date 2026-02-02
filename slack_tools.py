# slack_tools.py

import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
from typing import List, Dict
import random

load_dotenv()

# Initialize Slack client with Bot Token
slack_token = os.environ.get("SLACK_USER_TOKEN")
slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
if not slack_token:
    raise ValueError("SLACK_USER_TOKEN environment variable not set")

client = WebClient(token=slack_token)
bot_client = WebClient(token=slack_bot_token)


def list_channels(types: str = "public_channel,private_channel") -> List[Dict]:
    """
    Returns a list of channels the bot has access to.

    Parameters:
    - types (str): Channel types to include (default: public & private)

    Returns:
    - List of dictionaries with 'id' and 'name' of channels
    """
    try:
        response = client.conversations_list(types=types)
        channels = response.get("channels", [])
        return [{"id": c["id"], "name": c["name"]} for c in channels]

    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to list channels: {str(e)}")


def get_channel_id(channel: str) -> str:
    """
    Converts a channel name (e.g., #general) or ID to the channel ID.
    """
    if channel.startswith("#"):
        channel_name = channel[1:]
    else:
        channel_name = channel

    # If already an ID (starts with C, G, or D), return it
    if channel.startswith(("C", "G", "D")):
        return channel

    # Map name to ID
    channels = list_channels()
    for c in channels:
        if c["name"] == channel_name:
            return c["id"]

    raise ValueError(f"Channel '{channel}' not found or bot is not a member.")


def join_channel_if_needed(channel_id: str):
    """
    Joins a public channel if the bot is not already in it.
    """
    try:
        client.conversations_join(channel=channel_id)
    except SlackApiError as e:
        if e.response["error"] == "method_not_supported_for_channel_type":
            # Private channel - bot must be invited
            pass
        elif e.response["error"] == "already_in_channel":
            pass
        else:
            raise


def get_channel_messages(channel: str, limit: int = 50) -> List[str]:
    """
    Fetches messages from a given Slack channel by name or ID.

    Parameters:
    - channel (str): Slack channel ID or name (with or without #)
    - limit (int): Number of messages to fetch (default 50)

    Returns:
    - List of message strings in chronological order
    """
    try:
        channel_id = get_channel_id(channel)
        # Attempt to join public channels if not already in
        join_channel_if_needed(channel_id)

        response = client.conversations_history(channel=channel_id, limit=limit)
        messages = response.get("messages", [])
        messages.reverse()  # chronological order
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
    """
    Posts a message to a Slack channel.

    Parameters:
    - channel (str): Slack channel ID or name (with or without #)
    - message (str): Message text to post

    Returns:
    - Dictionary with channel and timestamp of posted message
    """
    try:
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        response = client.chat_postMessage(
            channel=channel_id,
            text=message
        )

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
    """
    Fetches threads from a Slack channel.

    Parameters:
    - channel (str): Slack channel ID or name (with or without #)
    - limit (int): Number of parent messages to inspect (default 20)

    Returns:
    - List of threads with parent message and replies
    """
    try:
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        # Step 1: Get channel messages
        history = client.conversations_history(
            channel=channel_id,
            limit=limit
        )

        threads = []

        for msg in history.get("messages", []):
            # Only messages that start threads
            if msg.get("reply_count", 0) > 0 and "ts" in msg:
                replies_resp = client.conversations_replies(
                    channel=channel_id,
                    ts=msg["ts"]
                )

                replies = replies_resp.get("messages", [])

                threads.append({
                    "thread_ts": msg["ts"],
                    "parent_text": msg.get("text", ""),
                    "reply_count": msg.get("reply_count", 0),
                    "replies": [
                        {
                            "user": r.get("user"),
                            "text": r.get("text"),
                            "ts": r.get("ts")
                        }
                        for r in replies[1:]  # skip parent message
                    ]
                })

        return threads

    except SlackApiError as e:
        raise ValueError(f"Slack API Error: {e.response['error']}")
    except Exception as e:
        raise ValueError(f"Failed to fetch threads: {str(e)}")


def reply_to_thread(channel: str, thread_ts: str, message: str) -> Dict:
    """
    Reply to a thread in a Slack channel.

    Parameters:
    - channel (str): Slack channel ID or name (with or without #)
    - thread_ts (str): Thread timestamp to reply to
    - message (str): Message text to post

    Returns:
    - Dictionary with channel, thread_ts, and timestamp of posted message
    """
    try:
        channel_id = get_channel_id(channel)
        join_channel_if_needed(channel_id)

        response = client.chat_postMessage(
            channel=channel_id,
            text=message,
            thread_ts=thread_ts
        )

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
    """
    Search Slack messages using a query string.

    Parameters:
    - query (str): Text to search for
    - limit (int): Maximum number of results to return

    Returns:
    - List of matching messages with channel, user, text, and timestamp
    """
    try:
        response = bot_client.search_messages(
            query=query,
            count=limit
        )

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
    """
    Prepare Slack channel messages for LLM-based summarization.
    No LLM call happens here.
    """
    messages = get_channel_messages(channel, limit)

    if not messages:
        return {
            "instructions": "No messages found in the channel.",
            "sampled_text": []
        }

    sampled_text = []

    for msg in messages:
        # Case 1: message is already a string
        if isinstance(msg, str):
            text = msg.strip()
            if text:
                sampled_text.append(text)

        # Case 2: message is a dict (Slack-style)
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

# slack_tools.py (add this)

import re
from typing import List, Dict

def extract_action_items(messages: List[str]) -> List[str]:
    """
    Extract actionable items from a list of Slack messages.
    Looks for messages containing common action verbs or patterns.
    
    Parameters:
    - messages: list of message strings
    
    Returns:
    - List of action item strings
    """
    action_verbs = [
        "assign", "complete", "review", "update", "follow up",
        "schedule", "submit", "plan", "fix", "check", "resolve"
    ]
    
    action_items = []

    for msg in messages:
        msg_lower = msg.lower()
        if any(verb in msg_lower for verb in action_verbs):
            # Optional: remove user mentions and special characters
            clean_msg = re.sub(r"<@[\w]+>", "", msg).strip()
            action_items.append(clean_msg)

    return action_items
