# MongoDB MCP Server + Streamlit Client

> Built by [Satish Lakkimsetti](https://github.com/satish-lakkimsetti) — *"Started from curiosity. Heading toward mastery."*

A fully containerized system consisting of:

1. **MCP Server** — a standalone, reusable MongoDB MCP server that exposes MongoDB operations as [Model Context Protocol](https://modelcontextprotocol.io/) tools over HTTP/SSE and stdio.
2. **Streamlit App** — an AI-powered chat client that connects to the MCP server and lets you query/modify MongoDB in natural language.

```
┌─────────────────────────────────────────────────────────────┐
│                      docker-compose                         │
│                                                             │
│  ┌──────────────────┐         ┌──────────────────────────┐  │
│  │   mcp-server     │◄────────│    streamlit-app          │  │
│  │  :8000           │  HTTP   │    :8501                  │  │
│  │                  │  /rpc   │                           │  │
│  │  JSON-RPC over   │         │  AI (OpenAI / Anthropic)  │  │
│  │  HTTP + SSE      │         │  ← tool-calling →         │  │
│  └───────┬──────────┘         └──────────────────────────┘  │
│          │                                                   │
└──────────┼───────────────────────────────────────────────────┘
           │ PyMongo
           ▼
   External MongoDB
   (not in compose)
```

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- An AI API key: OpenAI, or any OpenAI-compatible provider, or Anthropic
- A running MongoDB instance (see "Testing with a local MongoDB" below)

---

## Quick start

```bash
git clone https://github.com/satish-lakkimsetti/mongodb-mcp-server.git
cd mongodb-mcp-server

# Build and start both services
docker compose up --build
```

- **Streamlit UI:** http://localhost:8501  
- **MCP Server API:** http://localhost:8000

---

## Using the Streamlit app

1. **Open** http://localhost:8501 in your browser.
2. **Connect to MongoDB** — enter your MongoDB URI in the sidebar and click **Connect**. The app calls the MCP server's `connect` tool; your URI is held only in server memory for the session.
3. **Configure AI** — choose OpenAI-compatible or Anthropic, enter the base URL (for OpenAI-compatible), model name, and API key. The key is held for this browser session only and is never persisted.
4. **Chat** — type any question about your data, e.g.:
   - *"List all databases and their collections."*
   - *"How many documents are in the orders collection?"*
   - *"Find the top 5 customers by total order value."*
   - *"Insert a test document into mydb.test_col"* (triggers confirm dialog)
5. **Confirm writes** — for any insert/update/delete/admin operation, the UI presents a confirmation dialog showing the exact operation before it runs.
6. **Operation log** — every executed MCP tool call is recorded in the sidebar log.

---

## Testing with a local MongoDB container

Spin up a standalone MongoDB container on the same machine (separate from the compose stack):

```bash
docker run -d \
  --name test-mongo \
  -p 27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=admin \
  -e MONGO_INITDB_ROOT_PASSWORD=secret \
  mongo:7
```

**Connection URI for the Streamlit app:**

```
mongodb://admin:secret@host.docker.internal:27017/
```

> `host.docker.internal` resolves to your host machine from inside Docker containers (works on Docker Desktop for Mac/Windows). On Linux, use your machine's IP address or add `--network host` to the compose services.

**Load some sample data:**

```bash
docker exec -it test-mongo mongosh \
  -u admin -p secret --authenticationDatabase admin \
  --eval '
    db = db.getSiblingDB("shop");
    db.products.insertMany([
      {name: "Widget", price: 9.99, stock: 100},
      {name: "Gadget", price: 49.99, stock: 25},
      {name: "Doohickey", price: 4.99, stock: 500}
    ]);
    db.orders.insertMany([
      {product: "Widget", qty: 3, total: 29.97, customer: "alice"},
      {product: "Gadget", qty: 1, total: 49.99, customer: "bob"},
      {product: "Widget", qty: 10, total: 99.90, customer: "alice"}
    ]);
    print("Done.");
  '
```

---

## Manual walkthrough (validation)

### Read operations

1. Connect with `mongodb://admin:secret@host.docker.internal:27017/`
2. Chat: *"What databases and collections exist?"*  
   → AI calls `list_databases`, then `list_collections` for each DB.
3. Chat: *"Show me the schema of the shop.products collection."*  
   → AI calls `inspect_schema` and returns field names + types.
4. Chat: *"Find all orders by customer alice."*  
   → AI calls `find` with `{customer: "alice"}` and renders a table.
5. Chat: *"Count orders and total revenue by customer."*  
   → AI calls `aggregate` with a `$group` pipeline.

### Write/admin operations (confirm gate)

6. Chat: *"Insert a new product: Thingamajig, price 14.99, stock 75."*  
   → AI proposes `insert_one` with `confirmed=false`. Server returns a confirmation-required message. The UI shows a confirm dialog with the operation preview.  
   → Click **Confirm & Execute** → AI re-calls with `confirmed=true` → document inserted.
7. Chat: *"Delete all products with stock less than 30."*  
   → Confirm dialog appears before `delete_many` runs.
8. Chat: *"Drop the test_col collection."* (if created)  
   → Confirm dialog before `drop_collection`.

---

## External MCP clients (Claude Desktop, etc.)

The MCP server is a standalone service that any MCP client can use.

### Via HTTP/SSE (recommended for network clients)

Point your MCP client at the SSE endpoint:

```
SSE URL:      http://localhost:8000/sse
Messages URL: http://localhost:8000/messages
```

After connecting, call the `connect` tool with your URI:

```json
{
  "name": "connect",
  "arguments": { "uri": "mongodb://admin:secret@host.docker.internal:27017/" }
}
```

### Via stdio (Claude Desktop)

Run the server in stdio mode (no Docker needed for the server itself):

```bash
pip install pymongo uvicorn starlette
MCP_TRANSPORT=stdio python mcp-server/server.py
```

**Claude Desktop config** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mongodb": {
      "command": "python",
      "args": ["/absolute/path/to/mongodb-mcp-server/mcp-server/server.py"],
      "env": { "MCP_TRANSPORT": "stdio" }
    }
  }
}
```

After Claude Desktop connects, use the `connect` tool in any conversation:

> *"Connect to MongoDB at mongodb://admin:secret@localhost:27017/"*

Claude will call the `connect` tool automatically.

---

## Safety model

| Operation type | Examples | Confirmation |
|---|---|---|
| **Read** | `find`, `aggregate`, `count_documents`, `list_databases`, `list_collections`, `inspect_schema`, `get_indexes` | None — executes immediately |
| **Write / Admin** | `insert_one/many`, `update_one/many`, `delete_one/many`, `create_collection`, `drop_collection`, `create_index`, `drop_index` | **Server-enforced:** must pass `confirmed=true`; Streamlit UI also presents a confirm dialog |

The confirmation gate is enforced at the server level — any MCP client (Streamlit, Claude Desktop, custom) will receive a `confirmation_required` response if it calls a write tool without `confirmed=true`.

---

## Stopping and cleaning up

### MCP server stack

```bash
# Stop and keep the settings volume (reconnect later without re-entering credentials)
docker compose down

# Stop + delete the settings volume (full reset)
docker compose down -v

# Also remove the built images
docker rmi mongodb-mcp-server:latest mongodb-mcp-streamlit:latest
```

---

### Test MongoDB container

```bash
# Stop (data is preserved in named volumes)
docker stop test-mongo

# Stop and remove the container (volumes kept — restart later and data is still there)
docker rm -f test-mongo

# Remove the container AND its data volumes (full reset)
docker rm -f test-mongo
docker volume rm mongo-data mongo-config
```

> **Why do I see 3 volumes when I run test-mongo?**
>
> The `mongo:7` image declares two internal mount points (`/data/db` and `/data/configdb`) via Docker's `VOLUME` instruction. When you run MongoDB **without** explicit `-v` flags, Docker silently creates one anonymous (hash-named) volume per mount point. Always use named volumes to keep things tidy:
>
> ```bash
> docker run -d --name test-mongo \
>   -p 27017:27017 \
>   -e MONGO_INITDB_ROOT_USERNAME=admin \
>   -e MONGO_INITDB_ROOT_PASSWORD=secret \
>   -v mongo-data:/data/db \
>   -v mongo-config:/data/configdb \
>   mongo:7
> ```
>
> | Volume | Contains |
> |---|---|
> | `mongodb-mcp-server_streamlit-settings` | Streamlit settings JSON (credentials, op log) |
> | `mongo-data` | MongoDB data files |
> | `mongo-config` | Replica set config (empty for standalone) |

---

## Environment variables

| Variable | Service | Default | Description |
|---|---|---|---|
| `MCP_TRANSPORT` | mcp-server | `http` | Set to `stdio` for stdio mode |
| `MCP_HOST` | mcp-server | `0.0.0.0` | Bind address |
| `MCP_PORT` | mcp-server | `8000` | HTTP port |
| `MCP_SERVER_URL` | streamlit-app | `http://mcp-server:8000` | MCP server URL (inside Docker network) |

---

## Project layout

```
mongodb-mcp-server/
├── docker-compose.yml
├── README.md
├── mcp-server/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── server.py          # MCP server (HTTP JSON-RPC + SSE + stdio)
└── streamlit-app/
    ├── Dockerfile
    ├── requirements.txt
    └── app.py             # Streamlit AI chat client
```

---

## Author

**Satish Lakkimsetti** — *"Started from curiosity. Heading toward mastery."*

- GitHub: [satish-lakkimsetti](https://github.com/satish-lakkimsetti)
- Other projects: [local-rag-stack](https://github.com/satish-lakkimsetti/local-rag-stack) · [RefFLEXITY-CLI](https://github.com/satish-lakkimsetti/RefFLEXITY-CLI) · [NexyTab-Firefox](https://github.com/satish-lakkimsetti/NexyTab-Firefox)
