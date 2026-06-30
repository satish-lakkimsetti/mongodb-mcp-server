"""
MongoDB MCP Server

Exposes MongoDB operations as MCP tools over:
- HTTP/SSE (standard MCP SSE transport for external MCP clients)
- HTTP JSON-RPC (/rpc endpoint, for the bundled Streamlit client)
- stdio (for Claude Desktop and other stdio MCP clients)

Per-session connection model: each client identifies itself via a session_id.
Write/admin tools require confirmed=true to execute (server-enforced).
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

from pymongo import MongoClient
from pymongo.errors import PyMongoError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
import uvicorn

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-session connection registry
# ---------------------------------------------------------------------------

_sessions: dict[str, MongoClient] = {}


def _get_client(session_id: str) -> MongoClient:
    client = _sessions.get(session_id)
    if client is None:
        raise RuntimeError(
            "No MongoDB connection for this session. Call the 'connect' tool first "
            "with your MongoDB URI."
        )
    return client


def _drop_session(session_id: str) -> None:
    client = _sessions.pop(session_id, None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
        logger.info("Dropped MongoDB connection for session %s", session_id)


# ---------------------------------------------------------------------------
# Tool definitions (MCP schema format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "connect",
        "description": (
            "Establish a MongoDB connection for this session. "
            "Must be called before any other tool. The URI is held in memory only "
            "and is never persisted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "MongoDB connection URI e.g. mongodb://user:pass@host:27017/",
                }
            },
            "required": ["uri"],
        },
    },
    {
        "name": "disconnect",
        "description": "Close and drop the MongoDB connection for this session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ── Read operations ──────────────────────────────────────────────────────
    {
        "name": "list_databases",
        "description": "List all databases on the connected MongoDB server.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_collections",
        "description": "List all collections in a database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "Database name"}
            },
            "required": ["database"],
        },
    },
    {
        "name": "inspect_schema",
        "description": (
            "Sample documents from a collection and infer field structure. "
            "Returns field names and observed types only — no bulk data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "sample_size": {
                    "type": "integer",
                    "default": 20,
                    "description": "Documents to sample (max 100)",
                },
            },
            "required": ["database", "collection"],
        },
    },
    {
        "name": "find",
        "description": "Run a find query on a collection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "filter": {"type": "object", "default": {}},
                "projection": {"type": "object", "default": {}},
                "sort": {"type": "object", "default": {}},
                "limit": {"type": "integer", "default": 20, "description": "Max 200"},
                "skip": {"type": "integer", "default": 0},
            },
            "required": ["database", "collection"],
        },
    },
    {
        "name": "aggregate",
        "description": "Run an aggregation pipeline on a collection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "pipeline": {"type": "array", "description": "Aggregation stages"},
                "limit": {"type": "integer", "default": 50, "description": "Appended $limit (max 200)"},
            },
            "required": ["database", "collection", "pipeline"],
        },
    },
    {
        "name": "count_documents",
        "description": "Count documents matching a filter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "filter": {"type": "object", "default": {}},
            },
            "required": ["database", "collection"],
        },
    },
    {
        "name": "get_indexes",
        "description": "List indexes on a collection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
            },
            "required": ["database", "collection"],
        },
    },
    # ── Write / admin operations (confirmed=true required) ───────────────────
    {
        "name": "insert_one",
        "description": "Insert a single document. REQUIRES confirmed=true to execute.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "document": {"type": "object"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection", "document"],
        },
    },
    {
        "name": "insert_many",
        "description": "Insert multiple documents. REQUIRES confirmed=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "documents": {"type": "array", "items": {"type": "object"}},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection", "documents"],
        },
    },
    {
        "name": "update_one",
        "description": "Update the first matching document. REQUIRES confirmed=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "filter": {"type": "object"},
                "update": {"type": "object"},
                "upsert": {"type": "boolean", "default": False},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection", "filter", "update"],
        },
    },
    {
        "name": "update_many",
        "description": "Update all matching documents. REQUIRES confirmed=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "filter": {"type": "object"},
                "update": {"type": "object"},
                "upsert": {"type": "boolean", "default": False},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection", "filter", "update"],
        },
    },
    {
        "name": "delete_one",
        "description": "Delete the first matching document. REQUIRES confirmed=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "filter": {"type": "object"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection", "filter"],
        },
    },
    {
        "name": "delete_many",
        "description": "Delete all matching documents. REQUIRES confirmed=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "filter": {"type": "object"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection", "filter"],
        },
    },
    {
        "name": "create_collection",
        "description": "Create a new collection. REQUIRES confirmed=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection"],
        },
    },
    {
        "name": "drop_collection",
        "description": "Drop (permanently delete) a collection. REQUIRES confirmed=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection"],
        },
    },
    {
        "name": "create_index",
        "description": "Create an index on a collection. REQUIRES confirmed=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "keys": {"type": "object", "description": "Index key spec e.g. {field: 1}"},
                "options": {"type": "object", "default": {}},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection", "keys"],
        },
    },
    {
        "name": "drop_index",
        "description": "Drop an index by name. REQUIRES confirmed=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "index_name": {"type": "string"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["database", "collection", "index_name"],
        },
    },
]

WRITE_TOOLS = {
    "insert_one", "insert_many", "update_one", "update_many",
    "delete_one", "delete_many", "create_collection", "drop_collection",
    "create_index", "drop_index",
}


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

def _serialize(obj: Any) -> Any:
    import bson
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    if isinstance(obj, bson.ObjectId):
        return str(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def _confirm_required(tool_name: str, args: dict) -> str:
    safe_args = {k: v for k, v in args.items() if k not in ("confirmed", "__session_id__")}
    return json.dumps({
        "confirmation_required": True,
        "tool": tool_name,
        "message": (
            f"'{tool_name}' is a write/admin operation. "
            "Re-call with confirmed=true to execute."
        ),
        "operation_preview": safe_args,
    })


def execute_tool(tool_name: str, args: dict) -> str:
    session_id = args.pop("__session_id__", "default")
    try:
        if tool_name == "connect":
            uri = args["uri"]
            old = _sessions.pop(session_id, None)
            if old:
                try:
                    old.close()
                except Exception:
                    pass
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            client.admin.command("ping")
            _sessions[session_id] = client
            return json.dumps({"status": "connected", "session_id": session_id})

        if tool_name == "disconnect":
            _drop_session(session_id)
            return json.dumps({"status": "disconnected"})

        if tool_name in WRITE_TOOLS and not args.get("confirmed", False):
            return _confirm_required(tool_name, args)

        client = _get_client(session_id)

        if tool_name == "list_databases":
            return json.dumps(client.list_database_names())

        if tool_name == "list_collections":
            return json.dumps(client[args["database"]].list_collection_names())

        if tool_name == "inspect_schema":
            col = client[args["database"]][args["collection"]]
            n = min(int(args.get("sample_size", 20)), 100)
            docs = list(col.aggregate([{"$sample": {"size": n}}]))
            schema: dict[str, set] = {}
            for doc in docs:
                for k, v in doc.items():
                    schema.setdefault(k, set()).add(type(v).__name__)
            return json.dumps({
                "collection": args["collection"],
                "sampled": len(docs),
                "fields": {k: sorted(list(v)) for k, v in schema.items()},
            })

        if tool_name == "find":
            col = client[args["database"]][args["collection"]]
            limit = min(int(args.get("limit", 20)), 200)
            cursor = col.find(
                filter=args.get("filter") or {},
                projection=args.get("projection") or None,
            )
            sort = args.get("sort") or {}
            if sort:
                cursor = cursor.sort(list(sort.items()))
            cursor = cursor.skip(int(args.get("skip", 0))).limit(limit)
            return json.dumps([_serialize(d) for d in cursor])

        if tool_name == "aggregate":
            col = client[args["database"]][args["collection"]]
            pipeline = list(args["pipeline"])
            limit = min(int(args.get("limit", 50)), 200)
            pipeline.append({"$limit": limit})
            return json.dumps([_serialize(d) for d in col.aggregate(pipeline)])

        if tool_name == "count_documents":
            col = client[args["database"]][args["collection"]]
            return json.dumps({"count": col.count_documents(args.get("filter") or {})})

        if tool_name == "get_indexes":
            col = client[args["database"]][args["collection"]]
            return json.dumps([_serialize(i) for i in col.list_indexes()])

        if tool_name == "insert_one":
            col = client[args["database"]][args["collection"]]
            result = col.insert_one(args["document"])
            return json.dumps({"inserted_id": str(result.inserted_id)})

        if tool_name == "insert_many":
            col = client[args["database"]][args["collection"]]
            result = col.insert_many(args["documents"])
            return json.dumps({"inserted_ids": [str(i) for i in result.inserted_ids]})

        if tool_name == "update_one":
            col = client[args["database"]][args["collection"]]
            r = col.update_one(args["filter"], args["update"], upsert=args.get("upsert", False))
            return json.dumps({"matched": r.matched_count, "modified": r.modified_count, "upserted_id": str(r.upserted_id) if r.upserted_id else None})

        if tool_name == "update_many":
            col = client[args["database"]][args["collection"]]
            r = col.update_many(args["filter"], args["update"], upsert=args.get("upsert", False))
            return json.dumps({"matched": r.matched_count, "modified": r.modified_count})

        if tool_name == "delete_one":
            col = client[args["database"]][args["collection"]]
            r = col.delete_one(args["filter"])
            return json.dumps({"deleted": r.deleted_count})

        if tool_name == "delete_many":
            col = client[args["database"]][args["collection"]]
            r = col.delete_many(args["filter"])
            return json.dumps({"deleted": r.deleted_count})

        if tool_name == "create_collection":
            client[args["database"]].create_collection(args["collection"])
            return json.dumps({"created": args["collection"]})

        if tool_name == "drop_collection":
            client[args["database"]].drop_collection(args["collection"])
            return json.dumps({"dropped": args["collection"]})

        if tool_name == "create_index":
            col = client[args["database"]][args["collection"]]
            name = col.create_index(list(args["keys"].items()), **args.get("options", {}))
            return json.dumps({"index_name": name})

        if tool_name == "drop_index":
            col = client[args["database"]][args["collection"]]
            col.drop_index(args["index_name"])
            return json.dumps({"dropped": args["index_name"]})

        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except PyMongoError as e:
        return json.dumps({"error": f"MongoDB error: {e}"})
    except Exception as e:
        logger.exception("Tool execution error")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Starlette HTTP app (JSON-RPC + SSE)
# ---------------------------------------------------------------------------

async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def rpc_handler(request: Request) -> JSONResponse:
    """
    Minimal JSON-RPC 2.0 handler for MCP methods:
      tools/list  -> list tools
      tools/call  -> call a tool
    Used by the Streamlit client for simplicity.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})

    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        })

    if method == "tools/call":
        name = params.get("name", "")
        arguments = dict(params.get("arguments", {}))
        result_str = execute_tool(name, arguments)
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": result_str}],
            },
        })

    # MCP initialize handshake (for SSE clients)
    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mongodb-mcp", "version": "1.0.0"},
            },
        })

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


async def sse_handler(request: Request) -> Response:
    """
    SSE endpoint for standard MCP SSE transport (external clients).
    Streams a JSON-RPC session over SSE.
    """
    import asyncio
    from starlette.responses import StreamingResponse

    session_id = request.query_params.get("session_id", str(id(request)))

    async def event_stream():
        # Send endpoint event so client knows where to POST messages
        post_url = str(request.url).replace("/sse", "/messages") + f"?session_id={session_id}"
        yield f"event: endpoint\ndata: {post_url}\n\n"

        # Keep alive
        while True:
            await asyncio.sleep(15)
            yield ": keepalive\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def messages_handler(request: Request) -> JSONResponse:
    """POST endpoint used by SSE-transport MCP clients."""
    session_id = request.query_params.get("session_id", "sse-default")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "parse error"}, status_code=400)

    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mongodb-mcp", "version": "1.0.0"},
            },
        })

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = dict(params.get("arguments", {}))
        arguments["__session_id__"] = session_id
        result_str = execute_tool(name, arguments)
        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": result_str}]},
        })

    if method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})

    return JSONResponse({
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


def make_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/health", endpoint=health),
            Route("/rpc", endpoint=rpc_handler, methods=["POST"]),
            Route("/sse", endpoint=sse_handler),
            Route("/messages", endpoint=messages_handler, methods=["POST"]),
        ]
    )


# ---------------------------------------------------------------------------
# stdio transport (for Claude Desktop etc.)
# ---------------------------------------------------------------------------

async def run_stdio():
    """Run as a stdio MCP server (JSON-RPC over stdin/stdout)."""
    logger.info("Starting MongoDB MCP server in stdio mode")

    async def handle_line(line: str):
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            return

        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mongodb-mcp", "version": "1.0.0"},
                },
            }
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            name = params.get("name", "")
            arguments = dict(params.get("arguments", {}))
            arguments.setdefault("__session_id__", "stdio-default")
            result_str = execute_tool(name, arguments)
            resp = {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": result_str}]},
            }
        elif method in ("notifications/initialized", "notifications/cancelled"):
            return  # notifications: no response
        else:
            resp = {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_event_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            line = line_bytes.decode().strip()
            if line:
                await handle_line(line)
        except Exception as e:
            logger.error("stdio error: %s", e)
            break


if __name__ == "__main__":
    mode = os.environ.get("MCP_TRANSPORT", "http").lower()
    if mode == "stdio":
        asyncio.run(run_stdio())
    else:
        port = int(os.environ.get("MCP_PORT", "8000"))
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        logger.info("Starting MongoDB MCP server (HTTP) on %s:%s", host, port)
        app = make_app()
        uvicorn.run(app, host=host, port=port)
