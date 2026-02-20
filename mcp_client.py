# mcp_client.py

import sys
import asyncio
from contextlib import AsyncExitStack
from typing import Any, Optional, List, Dict
from pydantic import AnyUrl
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


class SlackMCPClientWithLLM:
    """
    MCP Client that connects to a Slack MCP server and supports an external LLM agent
    for automated tool selection, chaining, and iterative reasoning.
    """

    def __init__(self, agent, command: str = "python", args: Optional[List[str]] = None, env: Optional[dict] = None):
        self.agent = agent  # external LLM agent, must have ask_gemini(prompt)
        self._command = command
        self._args = args or ["mcp_server.py"]
        self._env = env
        self._session: Optional[ClientSession] = None
        self._exit_stack: AsyncExitStack = AsyncExitStack()

    async def connect(self):
        """Connect to MCP server using stdio transport"""
        server_params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env
        )
        stdio_transport = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        _stdio, _write = stdio_transport
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(_stdio, _write)
        )
        await self._session.initialize()
        print("✅ Connected to MCP Server")

    def session(self) -> ClientSession:
        if not self._session:
            raise ConnectionError("MCP session not initialized. Call connect() first.")
        return self._session

    # -------------------------
    # MCP Tool Calls
    # -------------------------
    async def list_tools(self) -> List[types.Tool]:
        """Return list of tools available on the MCP server"""
        result = await self.session().list_tools()
        return result.tools

    async def call_tool(self, tool_name: str, tool_input: Optional[dict] = None) -> Any:
        """Call a single MCP tool with parameters"""
        tool_input = tool_input or {}
        return await self.session().call_tool(tool_name, tool_input)

    async def list_prompts(self) -> List[types.Prompt]:
        result = await self.session().list_prompts()
        return result.prompts

    async def get_prompt(self, prompt_name: str, args: Dict[str, str]):
        result = await self.session().get_prompt(prompt_name, args)
        return result.messages

    async def read_resource(self, uri: str) -> Any:
        result = await self.session().read_resource(AnyUrl(uri))
        resource = result.contents[0]

        if isinstance(resource, types.TextResourceContents):
            if resource.mimeType == "application/json":
                import json
                return json.loads(resource.text)
            return resource.text

    async def handle_sampling_reply(self, payload: dict) -> str:
        """
        If the server sends a sampling payload (reply_to_thread),
        call the external LLM agent and return the completion.
        """
        if not payload or "sampled_text" not in payload or not payload["sampled_text"]:
            return "[Error] No messages to sample."

        sampled_text = payload["sampled_text"]
        instructions = payload.get("instructions", "")

        prompt = instructions + "\n\n" + "\n".join(sampled_text)
        # Call the agent's LLM
        reply = self.agent.ask_gemini(prompt)
        return reply

    # -------------------------
    # New: Tool list for LLM planning
    # -------------------------
    async def get_tool_list_summary(self) -> List[Dict[str, str]]:
        """
        Return a simple summary of tools (name + description) for LLM planning.
        """
        tools = await self.list_tools()
        return [{"name": t.name, "description": t.description} for t in tools]

    # -------------------------
    # Cleanup
    # -------------------------
    async def cleanup(self):
        await self._exit_stack.aclose()
        self._session = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()

    async def get_prompt(self, prompt_name: str, args: Dict[str, str]):
        result = await self.session().get_prompt(prompt_name, args)
        return result.messages

# -------------------------
# For testing
# -------------------------
async def main():
    from cli_agent import GeminiSlackAgent
    agent = GeminiSlackAgent()

    async with SlackMCPClientWithLLM(agent=agent) as client:
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools])

        tool_summary = await client.get_tool_list_summary()
        print("Tool Summary for LLM:", tool_summary)

        # Example: call list_channels
        channels = await client.call_tool("list_channels")
        print("Slack Channels:", channels)

        # Example: simulate reply_to_thread sampling
        example_payload = {
            "sampled_text": [
                "Hello team, any updates on the new release?",
                "We need to finalize the testing schedule."
            ],
            "instructions": "Write a concise reply to the thread."
        }
        llm_reply = await client.handle_sampling_reply(example_payload)
        print("LLM Reply:", llm_reply)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())