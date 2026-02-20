# cli_agent.py

import os
import asyncio
import json
from typing import Optional, Any
import google.generativeai as genai
from mcp_client import SlackMCPClientWithLLM
from dotenv import load_dotenv

load_dotenv()


class GeminiSlackAgent:
    """
    LLM-driven Slack agent with automatic tool selection, chaining, and iterative reasoning.
    """

    def __init__(self, model_name: str = "gemini-2.5-flash-lite"):
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Set GOOGLE_API_KEY environment variable first.")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model_name)
        self.mcp_client: Optional[SlackMCPClientWithLLM] = None
        self.running = True
        self.context: dict = {}  # store intermediate tool outputs

    async def connect_mcp(self):
        self.mcp_client = SlackMCPClientWithLLM(agent=self)
        await self.mcp_client.connect()
        print("✅ Connected to MCP server")

    def ask_gemini(self, prompt: str) -> str:
        """Send prompt to Gemini and return text"""
        try:
            response = self.model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"[Gemini Error] {str(e)}"

    def resolve_reference(self, ref: str) -> Any:
        """
        Resolve a reference string like $threads[0].replies[0].text
        """
        if not ref.startswith("$"):
            return ref

        ref = ref[1:]  # remove $
        parts = ref.replace("]", "").replace("[", ".").split(".")
        value = self.context.get(parts[0])
        if value is None:
            raise ValueError(f"Reference {ref} not found in context")

        for part in parts[1:]:
            if part.isdigit():
                value = value[int(part)]
            else:
                # Handle both dict and object attributes
                if isinstance(value, dict):
                    value = value.get(part)
                elif hasattr(value, part):
                    value = getattr(value, part)
                else:
                    raise ValueError(f"Cannot access {part} in {type(value)}")
        return value

    def extract_tool_result_content(self, result) -> Any:
        """
        Extract the actual content from MCP CallToolResult objects
        """
        if hasattr(result, 'content'):
            # Handle list of content items
            if hasattr(result.content, '__iter__') and not isinstance(result.content, str):
                extracted = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        # Try to parse as JSON first
                        try:
                            extracted.append(json.loads(item.text))
                        except (json.JSONDecodeError, TypeError):
                            extracted.append(item.text)
                    elif hasattr(item, 'model_dump'):
                        extracted.append(item.model_dump())
                    else:
                        extracted.append(str(item))
                return extracted[0] if len(extracted) == 1 else extracted
            # Handle single content item
            elif hasattr(result.content, 'text'):
                try:
                    return json.loads(result.content.text)
                except (json.JSONDecodeError, TypeError):
                    return result.content.text
            else:
                return str(result.content)
        return result

    def serialize_context(self) -> str:
        """
        Serialize context to JSON-safe format, handling MCP CallToolResult objects
        """
        def make_serializable(obj):
            """Convert objects to JSON-serializable format"""
            if hasattr(obj, 'content'):
                return self.extract_tool_result_content(obj)
            elif isinstance(obj, dict):
                return {k: make_serializable(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [make_serializable(item) for item in obj]
            elif hasattr(obj, 'model_dump'):
                return obj.model_dump()
            elif hasattr(obj, '__dict__'):
                return str(obj)
            else:
                return obj
        
        serializable_context = make_serializable(self.context)
        return json.dumps(serializable_context, indent=2, default=str)

    async def execute_plan(self, plan_json: str) -> bool:
        """
        Execute a JSON plan returned by Gemini.
        Returns True if plan indicates done, False if more steps required.
        """
        # Strip markdown code blocks if present
        plan_json = plan_json.strip()
        if plan_json.startswith("```json"):
            plan_json = plan_json[7:]  # remove ```json
        elif plan_json.startswith("```"):
            plan_json = plan_json[3:]  # remove ```
        
        if plan_json.endswith("```"):
            plan_json = plan_json[:-3]  # remove trailing ```
        
        plan_json = plan_json.strip()
        
        # Check if response is just text (no JSON)
        if not plan_json or not plan_json.startswith('{'):
            print(f"[Info] Gemini responded with text only: {plan_json}")
            return False  # continue conversation
        
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError as e:
            print(f"[Error] Failed to parse Gemini plan: {e}")
            print("Raw response:")
            print(plan_json)
            return True  # stop if invalid JSON

        actions = plan.get("actions", [])
        done = plan.get("done", True)  # default True
        
        for action in actions:
            tool = action.get("tool")
            args = action.get("args", {})
            save_as = action.get("save_as")

            print(f"[Debug] Calling tool: {tool} with args: {args}")

            # Resolve $context references
            for k, v in args.items():
                if isinstance(v, str) and v.startswith("$"):
                    try:
                        args[k] = self.resolve_reference(v)
                        print(f"[Debug] Resolved ${v} to: {args[k]}")
                    except Exception as e:
                        print(f"[Error] {e}")
                        args[k] = None

            # Call the MCP tool
            try:
                result = await self.mcp_client.call_tool(tool, args)
                print(f"[Debug] Tool result: {result}")
                
                # Store result and extract content
                if save_as:
                    self.context[save_as] = result
                    # Also store the extracted content
                    extracted = self.extract_tool_result_content(result)
                    self.context[f"{save_as}_data"] = extracted
                    print(f"[Debug] Saved to context['{save_as}_data'] = {extracted}")
                            
            except Exception as e:
                print(f"[Error calling tool {tool}]: {e}")
                import traceback
                traceback.print_exc()

        # Print final response
        final_response = plan.get("response")
        if final_response:
            print(f"\n{final_response}\n")
        return done

    async def handle_user_input(self, user_input: str):
        """Automatically plan and execute tool usage for the user query"""
        if not self.mcp_client:
            raise RuntimeError("MCP client not connected")

        # Step 1: Get tool list dynamically for Gemini
        tool_summary = await self.mcp_client.get_tool_list_summary()
        tool_summary_json = json.dumps(tool_summary, indent=2)

        # Iterative reasoning loop
        done = False
        iteration = 0
        while not done and iteration < 5:  # limit iterations to prevent infinite loop
            iteration += 1
            
            # Serialize context safely
            context_json = self.serialize_context()
            
            prompt = f"""
You are a Slack assistant. The user asked: "{user_input}".
Available tools (latest from MCP server):
{tool_summary_json}

Current context from previous tool calls:
{context_json}

Return a JSON plan in this exact format (no extra text):
{{
  "actions": [
    {{
      "tool": "<tool_name>",
      "args": {{"param": "value"}},
      "save_as": "<optional_context_name>"
    }}
  ],
  "response": "<final text to show user with ACTUAL DATA, not templates>",
  "done": true
}}

CRITICAL RULES:
1. ALWAYS return valid JSON, nothing else
2. When showing data to user in "response", use the ACTUAL values from context, NOT template syntax like {{{{variable}}}}
3. Format lists as plain text like "• Channel1\\n• Channel2" NOT templates
4. If you need data, first fetch it with a tool action, then in the next iteration use that data
5. Use $context_name_data to reference extracted tool results
6. For channel names in args, use just the name without # (e.g., "social" not "#social")
7. For threads, the thread_ts is in the format like "1234567890.123456"
"""
            print(f"\n[Iteration {iteration}]")
            plan_json = self.ask_gemini(prompt)
            done = await self.execute_plan(plan_json)

    async def run_async(self):
        await self.connect_mcp()
        print("Gemini Slack Agent ready — type 'exit' to quit.")
        while self.running:
            try:
                user_input = await asyncio.to_thread(input, "> ")
                user_input = user_input.strip()
                if not user_input:
                    continue
                if user_input.lower() == "exit":
                    self.running = False
                    break
                await self.handle_user_input(user_input)
            except KeyboardInterrupt:
                print("\nExiting.")
                self.running = False
                break
            except Exception as e:
                print(f"[Error] {e}")
                import traceback
                traceback.print_exc()

        if self.mcp_client:
            await self.mcp_client.cleanup()
            print("✅ Disconnected from MCP server")

    def run(self):
        asyncio.run(self.run_async())


if __name__ == "__main__":
    agent = GeminiSlackAgent()
    agent.run()