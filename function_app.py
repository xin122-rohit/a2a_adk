# --------------------------------------------------------------
#  function_app.py  (Azure Functions v2 – single file)
# --------------------------------------------------------------
import azure.functions as func
import json
import time
import logging
from azure.identity import DefaultAzureCredential
import requests
import os

# ------------------------------------------------------------------
# 1. Azure AI Studio configuration
# ------------------------------------------------------------------
PROJECT_NAME = "agent-to-agent-5055"
AI_SERVICE_NAME = "agent-to-agent-5055-resource"
ASSISTANT_ID = "asst_zQ8ANX9CJfElxVlHKEKiLa5P"
API_VERSION = "2025-05-01"
BASE_URL = f"https://{AI_SERVICE_NAME}.services.ai.azure.com/api/projects/{PROJECT_NAME}"

# ------------------------------------------------------------------
# 2. Function App
# ------------------------------------------------------------------
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ------------------------------------------------------------------
# 3. GET  /api/chat/.well-known/agent-card.json  → A2A discovery
# ------------------------------------------------------------------
@app.route(route="chat/.well-known/agent-card.json", methods=["GET"])
def get_agent_card(req: func.HttpRequest) -> func.HttpResponse:
    """
    A2A v0.3.0 requires:
      • id & tags in every skill
      • preferredTransport = "HTTP"   (not JSON-RPC)
    """
    card = {
        "name": "Capital Agent",
        "description": "Answers capital-city questions for any country",
        "version": "1.0.0",
        "url": "https://func-a2a5055-a6fufrexfuaed0f7.eastus2-01.azurewebsites.net/api/chat",
        "preferredTransport": "JSONRPC",                 # <-- CRITICAL
        "protocolVersion": "0.3.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "capital_query",               # <-- REQUIRED
                "name": "capital_query",
                "description": "Answers capital-city questions",
                "inputModes": ["text"],
                "outputModes": ["text"],
                "tags": ["capital", "country"]        # <-- REQUIRED
            }
        ]
    }

    resp = func.HttpResponse(
        json.dumps(card),
        mimetype="application/json",
        status_code=200
    )
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ------------------------------------------------------------------
# 4. Helper – Azure AI token
# ------------------------------------------------------------------
def get_token() -> str:
    return DefaultAzureCredential().get_token("https://ai.azure.com").token


def call_api(method: str, url: str, json_body: dict | None = None):
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json"
    }
    resp = requests.request(method, url, headers=headers, json=json_body)
    resp.raise_for_status()
    return resp.json()

def _jsonrpc_error(code: int, message: str, request_id=None):
    resp = {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": request_id
    }
    return func.HttpResponse(
        json.dumps(resp),
        mimetype="application/json",
        status_code=200,
        headers={"Access-Control-Allow-Origin": "*"}
    )

def _jsonrpc_success(result: dict, request_id):
    resp = {
        "jsonrpc": "2.0",
        "result": result,
        "id": request_id
    }
    return func.HttpResponse(
        json.dumps(resp),
        mimetype="application/json",
        status_code=200,
        headers={"Access-Control-Allow-Origin": "*"}
    )
#------------------------------------------------------------
# 5. POST /api/chat  →  A2A SendMessageRequest (raw HTTP)
# ------------------------------------------------------------------
@app.route(route="chat", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def chat(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = req.get_json()
    except Exception:
        return _jsonrpc_error(-32700, "Parse error")

    if not isinstance(payload, dict):
        return _jsonrpc_error(-32600, "Invalid Request")
    if payload.get("jsonrpc") != "2.0":
        return _jsonrpc_error(-32600, "Invalid JSON-RPC version")
    if "method" not in payload or "id" not in payload:
        return _jsonrpc_error(-32600, "Missing method or id")

    method = payload["method"]
    request_id = payload["id"]
    params = payload.get("params", {})

    # === SUPPORT message/send AND sendMessage ===
    if method not in ["sendMessage", "message/send"]:
        return _jsonrpc_error(-32601, f"Method not found: {method}", request_id)

    # === Extract text from parts SAFELY ===
    message = params.get("message", {})
    parts = message.get("parts", [])
    user_text = ""

    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
            user_text = part["text"]
            break

    if not user_text:
        print(f"DEBUG: Received payload: {json.dumps(payload, indent=2)}")
        return _jsonrpc_error(-32602, "Invalid params: no valid text part", request_id)

    # === Call Azure AI Studio ===
    try:
        thread = call_api("POST", f"{BASE_URL}/threads?api-version={API_VERSION}", {})
        thread_id = thread["id"]

        call_api("POST", f"{BASE_URL}/threads/{thread_id}/messages?api-version={API_VERSION}",
                 {"role": "user", "content": user_text})

        run = call_api("POST", f"{BASE_URL}/threads/{thread_id}/runs?api-version={API_VERSION}",
                       {"assistant_id": ASSISTANT_ID})
        run_id = run["id"]

        while True:
            status_resp = call_api("GET", f"{BASE_URL}/threads/{thread_id}/runs/{run_id}?api-version={API_VERSION}")
            status = status_resp["status"]
            if status == "completed": break
            if status in ["failed", "cancelled"]:
                raise RuntimeError(f"Run {status}")
            time.sleep(1.5)

        messages = call_api("GET", f"{BASE_URL}/threads/{thread_id}/messages?api-version={API_VERSION}")
        reply_msg = next((m for m in messages["data"] if m["role"] == "assistant"), None)
        reply_text = reply_msg["content"][0]["text"]["value"] if reply_msg else "No reply"

    except Exception as e:
        logging.exception("AI call failed")
        return _jsonrpc_error(-32000, str(e), request_id)

    # === Return Success ===
    a2a_result = {
        "result": {
            "role": "assistant",
            "parts": [{"type": "text", "text": reply_text}]
        }
    }

    return _jsonrpc_success(a2a_result, request_id)