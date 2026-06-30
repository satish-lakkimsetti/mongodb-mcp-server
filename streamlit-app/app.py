"""
MongoDB MCP Streamlit Client

- Connects to the MCP server over HTTP/SSE
- Uses AI (OpenAI-compatible or Anthropic) with native tool-calling to translate
  natural language into MongoDB operations via the MCP server
- Presents confirm dialogs for write/admin operations before they execute
"""

import json
import os
import time
import uuid
from typing import Any

import httpx
import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WRITE_TOOLS = {
    "insert_one", "insert_many", "update_one", "update_many",
    "delete_one", "delete_many", "create_collection", "drop_collection",
    "create_index", "drop_index",
}

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://mcp-server:8000")
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/app/.mcp_settings.json")

# ---------------------------------------------------------------------------
# Settings persistence (file-based)
# ---------------------------------------------------------------------------

_PERSIST_KEYS = ["mongo_uri", "ai_provider", "ai_base_url", "ai_api_key", "ai_model"]
MAX_LOG_ENTRIES = 200


def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings() -> None:
    try:
        data = {k: st.session_state.get(k, "") for k in _PERSIST_KEYS}
        data["op_log"] = st.session_state.get("op_log", [])[-MAX_LOG_ENTRIES:]
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def init_state():
    if "initialized" not in st.session_state:
        saved = _load_settings()
        st.session_state["initialized"] = True
        st.session_state["op_log"] = saved.get("op_log", [])
        st.session_state.setdefault("messages", [])
        st.session_state.setdefault("mcp_session_id", str(uuid.uuid4()))
        st.session_state.setdefault("mongo_connected", False)
        st.session_state.setdefault("pending_confirm", None)
        st.session_state.setdefault("confirm_result", None)
        st.session_state["mongo_uri"] = saved.get("mongo_uri", "")
        st.session_state["ai_provider"] = saved.get("ai_provider", "openai") or "openai"
        st.session_state["ai_base_url"] = saved.get("ai_base_url", "https://api.openai.com/v1") or "https://api.openai.com/v1"
        st.session_state["ai_api_key"] = saved.get("ai_api_key", "")
        st.session_state["ai_model"] = saved.get("ai_model", "gpt-4o") or "gpt-4o"

init_state()

# ---------------------------------------------------------------------------
# MCP Client (HTTP/SSE — simplified JSON-RPC over HTTP POST + SSE)
# ---------------------------------------------------------------------------

class MCPClient:
    """
    Lightweight MCP client that speaks the JSON-RPC protocol over
    HTTP. We use the /sse endpoint for the SSE channel and /messages/
    for posting requests.

    For Streamlit (sync context) we use httpx sync client.
    """

    def __init__(self, base_url: str, session_id: str):
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self._msg_id = 0
        self._initialized = False
        self._tools_cache: list[dict] | None = None

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _post_message(self, method: str, params: dict | None = None) -> Any:
        """
        Post a JSON-RPC message to /messages/ and collect the SSE response.
        Because the MCP SSE transport requires the client to be listening on
        the SSE stream while posting, we use a simpler approach: we call a
        thin HTTP JSON-RPC endpoint that the server exposes synchronously.

        We implement a direct REST-like helper: send the request via POST to
        /call and receive the result synchronously.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        url = f"{self.base_url}/rpc"
        try:
            resp = httpx.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(data["error"].get("message", str(data["error"])))
            return data.get("result")
        except httpx.HTTPError as e:
            raise RuntimeError(f"MCP HTTP error: {e}")

    def list_tools(self) -> list[dict]:
        if self._tools_cache is not None:
            return self._tools_cache
        result = self._post_message("tools/list")
        self._tools_cache = result.get("tools", [])
        return self._tools_cache

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self._post_message("tools/call", {
            "name": name,
            "arguments": {**arguments, "__session_id__": self.session_id},
        })
        # result is {content: [{type: text, text: ...}]}
        content = result.get("content", [])
        if content:
            return content[0].get("text", "")
        return ""


@st.cache_resource
def _get_mcp_client_cached(session_id: str) -> MCPClient:
    return MCPClient(MCP_SERVER_URL, session_id)


def get_mcp_client() -> MCPClient:
    return _get_mcp_client_cached(st.session_state["mcp_session_id"])


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------

def mcp_call(tool: str, args: dict) -> str:
    client = get_mcp_client()
    result_str = client.call_tool(tool, args)
    st.session_state["op_log"].append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool": tool,
        "args": args,
        "result": result_str[:500],
    })
    _save_settings()
    return result_str


def check_server_health() -> bool:
    try:
        resp = httpx.get(f"{MCP_SERVER_URL}/health", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def get_tools_for_ai() -> list[dict]:
    """Fetch MCP tools and convert to OpenAI function-calling format."""
    try:
        client = get_mcp_client()
        tools = client.list_tools()
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# AI integration
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a helpful MongoDB assistant. You have access to MongoDB tools via MCP.

The user has already connected to MongoDB. Use the available tools to:
1. First inspect schema (list_databases, list_collections, inspect_schema) to understand the data structure
2. Then answer the user's question by querying or modifying the database

For read operations (find, aggregate, count, list_*): execute them directly.
For write/admin operations (insert_*, update_*, delete_*, create_*, drop_*): always set confirmed=false first and describe what you're about to do. The UI will ask the user to confirm before you retry with confirmed=true.

Be concise and helpful. Format query results clearly."""


def call_ai_openai(messages: list, tools: list) -> Any:
    """Call OpenAI-compatible API with tool use."""
    import openai
    client = openai.OpenAI(
        api_key=st.session_state["ai_api_key"],
        base_url=st.session_state["ai_base_url"],
    )
    response = client.chat.completions.create(
        model=st.session_state["ai_model"],
        messages=messages,
        tools=tools or None,
        tool_choice="auto" if tools else None,
    )
    return response.choices[0].message


def call_ai_anthropic(messages: list, tools: list) -> Any:
    """Call Anthropic API with tool use."""
    import anthropic

    # Convert OpenAI tool format to Anthropic format
    anthropic_tools = [
        {
            "name": t["function"]["name"],
            "description": t["function"]["description"],
            "input_schema": t["function"]["parameters"],
        }
        for t in tools
    ] if tools else []

    # Split system from messages
    system = SYSTEM_PROMPT
    anthro_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            anthro_msgs.append(m)

    client = anthropic.Anthropic(api_key=st.session_state["ai_api_key"])
    response = client.messages.create(
        model=st.session_state["ai_model"],
        max_tokens=4096,
        system=system,
        messages=anthro_msgs,
        tools=anthropic_tools or anthropic.NOT_GIVEN,
    )
    return response


def normalize_anthropic_response(response: Any) -> dict:
    """Normalize Anthropic response to OpenAI-like dict."""
    text_parts = []
    tool_calls = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                },
            })
    return {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
        "tool_calls": tool_calls if tool_calls else None,
        "stop_reason": response.stop_reason,
    }


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------

def _step_openai(messages: list, tools: list) -> tuple[str | None, list]:
    """
    One OpenAI turn. Returns (final_text, tool_calls_list).
    Appends the raw SDK message to `messages` in-place.
    tool_calls_list entries: (id, name, args_dict)
    """
    raw = call_ai_openai(messages, tools)
    messages.append(raw)  # raw SDK object — avoids Pydantic v2 re-validation
    if not raw.tool_calls:
        return raw.content or "", []
    calls = [(tc.id, tc.function.name, json.loads(tc.function.arguments)) for tc in raw.tool_calls]
    return None, calls


def _step_anthropic(messages: list, tools: list) -> tuple[str | None, list]:
    """
    One Anthropic turn. Returns (final_text, tool_calls_list).
    Appends {"role": "assistant", "content": <content blocks>} to `messages` in-place.
    Anthropic requires content blocks (not a tool_calls key) in assistant messages.
    tool_calls_list entries: (id, name, args_dict)
    """
    raw = call_ai_anthropic(messages, tools)
    # Append raw content blocks — this is what Anthropic expects back in the next turn
    messages.append({"role": "assistant", "content": raw.content})
    tool_use_blocks = [b for b in raw.content if b.type == "tool_use"]
    if not tool_use_blocks:
        text_parts = [b.text for b in raw.content if b.type == "text"]
        return "\n".join(text_parts), []
    calls = [(b.id, b.name, b.input) for b in tool_use_blocks]
    return None, calls


def _append_tool_result(messages: list, provider: str, tc_id: str, result: str) -> None:
    if provider == "anthropic":
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tc_id, "content": result}],
        })
    else:
        messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})


def _agent_step(messages: list, tools: list, provider: str) -> tuple[str | None, list]:
    if provider == "anthropic":
        return _step_anthropic(messages, tools)
    return _step_openai(messages, tools)


def run_agentic_loop(user_message: str) -> str:
    tools = get_tools_for_ai()
    provider = st.session_state["ai_provider"]

    history = st.session_state["messages"][-20:]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + [
        {"role": m["role"], "content": m["content"]} for m in history
        if m["role"] in ("user", "assistant") and isinstance(m["content"], str)
    ]
    messages.append({"role": "user", "content": user_message})

    for _ in range(10):
        final_text, tool_calls = _agent_step(messages, tools, provider)
        if final_text is not None:
            return final_text

        for tc_id, fn_name, fn_args in tool_calls:
            if fn_name in WRITE_TOOLS and not fn_args.get("confirmed", False):
                st.session_state["pending_confirm"] = {
                    "tool": fn_name, "args": fn_args, "call_id": tc_id,
                    "messages": messages, "tools": tools, "provider": provider,
                }
                return f"__PENDING_CONFIRM__{fn_name}"

            result = mcp_call(fn_name, fn_args)
            _append_tool_result(messages, provider, tc_id, result)

    return "Reached maximum iterations. Please try a more specific question."


def resume_after_confirm(confirmed: bool) -> str:
    pending = st.session_state.get("pending_confirm")
    if not pending:
        return "No pending operation."

    messages = pending["messages"]
    tools = pending["tools"]
    provider = pending["provider"]
    fn_name = pending["tool"]
    fn_args = dict(pending["args"])
    tc_id = pending["call_id"]
    st.session_state["pending_confirm"] = None

    if not confirmed:
        return f"Operation `{fn_name}` was cancelled by the user."

    fn_args["confirmed"] = True
    result = mcp_call(fn_name, fn_args)
    _append_tool_result(messages, provider, tc_id, result)

    for _ in range(8):
        final_text, tool_calls = _agent_step(messages, tools, provider)
        if final_text is not None:
            return final_text
        for tc_i, fn_n, fn_a in tool_calls:
            res = mcp_call(fn_n, fn_a)
            _append_tool_result(messages, provider, tc_i, res)

    return "Done."


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def render_result(result_str: str):
    """Render a JSON result string as a table if it's a list of dicts."""
    try:
        data = json.loads(result_str)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            df = pd.DataFrame(data)
            st.dataframe(df, use_container_width=True)
            return
        if isinstance(data, dict):
            st.json(data)
            return
    except Exception:
        pass
    st.text(result_str)


# ---------------------------------------------------------------------------
# Sidebar: configuration
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("MongoDB MCP")

    # --- Server status ---
    st.subheader("MCP Server")
    if st.button("Check server"):
        if check_server_health():
            st.success("Server is up")
        else:
            st.error("Server unreachable")

    # --- MongoDB connection ---
    st.subheader("MongoDB Connection")
    uri_input = st.text_input(
        "Connection URI",
        value=st.session_state["mongo_uri"],
        type="password",
        placeholder="mongodb://user:pass@host:27017/",
    )
    if st.button("Connect"):
        try:
            result = mcp_call("connect", {"uri": uri_input})
            data = json.loads(result)
            if "error" in data:
                st.error(data["error"])
            else:
                st.session_state["mongo_uri"] = uri_input
                st.session_state["mongo_connected"] = True
                _save_settings()
                st.success("Connected!")
        except Exception as e:
            st.error(str(e))

    if st.session_state["mongo_connected"]:
        st.success("MongoDB: Connected")
        if st.button("Disconnect"):
            mcp_call("disconnect", {})
            st.session_state["mongo_connected"] = False
            st.session_state["mongo_uri"] = ""
    else:
        st.warning("MongoDB: Not connected")

    st.divider()

    # --- AI configuration ---
    st.subheader("AI Configuration")
    provider = st.selectbox(
        "Provider",
        ["openai", "anthropic"],
        index=0 if st.session_state["ai_provider"] == "openai" else 1,
    )
    st.session_state["ai_provider"] = provider

    if provider == "openai":
        base_url = st.text_input("Base URL", value=st.session_state["ai_base_url"])
        st.session_state["ai_base_url"] = base_url
        model = st.text_input("Model", value=st.session_state["ai_model"])
    else:
        default_model = st.session_state["ai_model"] if not st.session_state["ai_model"].startswith("gpt") else "claude-opus-4-8"
        model = st.text_input("Model", value=default_model)

    st.session_state["ai_model"] = model

    key_input = st.text_input("API Key", type="password", value=st.session_state["ai_api_key"])
    if key_input:
        st.session_state["ai_api_key"] = key_input

    if st.button("Save AI Settings"):
        _save_settings()
        st.success("Settings saved!")

    if st.session_state["ai_api_key"]:
        st.success("AI: Configured")
    else:
        st.warning("AI: No API key")

    st.divider()

    # --- Operation log ---
    st.subheader("Operation Log")
    if st.session_state["op_log"]:
        for entry in reversed(st.session_state["op_log"][-10:]):
            with st.expander(f"{entry['time']} — {entry['tool']}"):
                st.json({"args": entry["args"], "result": entry["result"]})
    else:
        st.caption("No operations yet.")

    if st.button("Clear log"):
        st.session_state["op_log"] = []
        _save_settings()


# ---------------------------------------------------------------------------
# Main chat area
# ---------------------------------------------------------------------------

st.title("MongoDB AI Assistant")

# Guard: need AI key configured
if not st.session_state["ai_api_key"]:
    st.info("Enter your AI API key in the sidebar to start chatting.")
    st.stop()

if not st.session_state["mongo_connected"]:
    st.info("Connect to MongoDB via the sidebar before chatting.")

# Display existing messages
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        content = msg["content"]
        if isinstance(content, str) and content.startswith("__PENDING_CONFIRM__"):
            st.warning("Waiting for confirmation...")
        else:
            st.markdown(content if isinstance(content, str) else str(content))

# Pending confirmation dialog
if st.session_state.get("pending_confirm"):
    pending = st.session_state["pending_confirm"]
    st.warning(
        f"**Confirmation required** — `{pending['tool']}` is a write/admin operation.\n\n"
        f"**Arguments:**\n```json\n{json.dumps({k: v for k, v in pending['args'].items() if k != 'confirmed'}, indent=2)}\n```"
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Confirm & Execute", type="primary"):
            with st.spinner("Executing..."):
                reply = resume_after_confirm(True)
            st.session_state["messages"].append({"role": "assistant", "content": reply})
            st.rerun()
    with col2:
        if st.button("Cancel"):
            reply = resume_after_confirm(False)
            st.session_state["messages"].append({"role": "assistant", "content": reply})
            st.rerun()

# Chat input
if prompt := st.chat_input("Ask anything about your MongoDB data..."):
    if not st.session_state["ai_api_key"]:
        st.error("Configure an AI API key in the sidebar first.")
    else:
        # Add user message
        st.session_state["messages"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    reply = run_agentic_loop(prompt)
                    if reply.startswith("__PENDING_CONFIRM__"):
                        tool_name = reply.replace("__PENDING_CONFIRM__", "")
                        display = f"I'd like to run `{tool_name}`. Please confirm in the dialog above."
                    else:
                        display = reply
                    st.markdown(display)
                    st.session_state["messages"].append({"role": "assistant", "content": display})
                except Exception as e:
                    err = f"Error: {e}"
                    st.error(err)
                    st.session_state["messages"].append({"role": "assistant", "content": err})
        st.rerun()
