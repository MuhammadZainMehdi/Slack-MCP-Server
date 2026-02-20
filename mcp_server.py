# mcp_server.py

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
    extract_action_items,
)

# Initialize MCP server
mcp = FastMCP("SlackMCP", log_level="ERROR")


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
    """
    MCP tool that fetches messages from Slack channel.
    """
    return get_channel_messages(channel, limit)


@mcp.tool(name="list_channels", description="List all channels the bot has access to")
def fetch_channels():
    """
    MCP tool that lists Slack channels available to the bot.
    """
    return list_channels()


@mcp.tool(
    name="post_message",
    description="Post a message to a Slack channel. Parameters: channel (string), message (string)",
)
def send_message(
    channel: str = Field(description="Channel name WITHOUT the # symbol"),
    message: str = Field(description="The text message to post to the channel"),
):
    """
    MCP tool that posts a message to a Slack channel.
    """
    return post_message(channel, message)


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
    """
    MCP tool that fetches threads from a Slack channel.
    """
    return get_threads(channel, limit)


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
    """
    MCP tool that replies to a thread in a Slack channel.
    """
    return slack_reply_to_thread(channel, thread_ts, message)


@mcp.tool(
    name="search_messages",
    description="Search messages across Slack channels by text query",
)
def search_slack_messages(
    query: str = Field(description="Search query text (e.g., 'deployment failure')"),
    limit: int = Field(default=20, description="Maximum number of results"),
):
    """
    MCP tool that searches Slack messages.
    """
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
    """
    MCP sampling tool:
    - Server gathers messages
    - Client LLM generates summary
    """
    return summarize_channel_source(channel, limit)


@mcp.tool(
    name="extract_action_items",
    description="Extract actionable items from a Slack channel",
)
def extract_items_tool(
    channel: str,
    limit: int = Field(
        default=50, description="Number of messages to fetch (default 50)"
    ),
):
    """
    MCP tool to extract action items from the latest messages of a Slack channel.
    """

    # Fetch recent messages
    messages = get_channel_messages(channel, limit)
    if not messages:
        return {"status": "empty", "message": "No messages to analyze"}

    # Extract action items
    items = extract_action_items(messages)
    return {"channel": channel, "action_items": items}


# MCP Prompts
@mcp.prompt(
    name="daily_channel_summary",
    description="Generate a daily summary of a Slack channel, focusing on important messages and decisions.",
)
async def daily_channel_summary(channel: str, limit: int = 50):
    from slack_tools import summarize_channel_source

    payload = summarize_channel_source(channel, limit)

    if payload["sampled_text"]:
        # Combine instructions + sampled text into a single string for LLM
        prompt_text = (
            payload["instructions"] + "\n\n" + "\n".join(payload["sampled_text"])
        )
        return prompt_text
    else:
        return "No messages to summarize."


@mcp.prompt(
    name="action_items_summary",
    description="Extract actionable items from a Slack channel or thread.",
)
async def action_items_summary(channel: str, limit: int = 50):
    from slack_tools import extract_action_items, get_channel_messages

    messages = get_channel_messages(channel, limit)
    if not messages:
        return "No messages in channel."

    items = extract_action_items(messages)
    if not items:
        return "No action items found."

    # Return a list of action items as strings
    return items


@mcp.prompt(
    name="thread_followup",
    description="Summarize a Slack thread and optionally generate a reply using client LLM.",
)
async def thread_followup(channel: str, thread_ts: str):
    from slack_tools import get_threads

    threads = get_threads(channel, limit=50)
    thread = next((t for t in threads if t["parent_ts"] == thread_ts), None)
    if not thread:
        return "Thread not found."

    messages = [thread["parent_text"]] + [r["text"] for r in thread.get("replies", [])]

    # Combine instructions + messages into a single string for LLM
    prompt_text = (
        "Summarize the thread and optionally suggest a reply:\n\n" + "\n".join(messages)
    )
    return prompt_text


import os
import uvicorn

app = mcp.streamable_http_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
