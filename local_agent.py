# --------------------------------------------------------------
# api.py  –  FastAPI version of your Azure Function
# --------------------------------------------------------------
from typing import Optional
import uuid
import json
import time
import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
# from azure.identity import DefaultAzureCredential
import requests
import os
from dotenv import load_dotenv

load_dotenv()  # optional – loads .env file if you have one

# ------------------------------------------------------------------
# 1. Azure AI Studio config (same as your function)
# ------------------------------------------------------------------
PROJECT_NAME = os.getenv("PROJECT_NAME", "agent-to-agent-5055")
AI_SERVICE_NAME = os.getenv("AI_SERVICE_NAME", "agent-to-agent-5055-resource")
ASSISTANT_ID = os.getenv("ASSISTANT_ID", "asst_zQ8ANX9CJfElxVlHKEKiLa5P")
API_VERSION = os.getenv("API_VERSION", "2025-05-01")
BASE_URL = f"https://{AI_SERVICE_NAME}.services.ai.azure.com/api/projects/{PROJECT_NAME}"

app = FastAPI()

# ------------------------------------------------------------------
# 2. Helper – Azure token
# ------------------------------------------------------------------
# ------------------------------------------------------------------
MANUAL_BEARER_TOKEN = os.getenv("AZURE_AI_BEARER_TOKEN")   # <-- set this in .env or shell

def get_token() -> str:
    """
    Returns a Bearer token.
    - If MANUAL_BEARER_TOKEN is set → use it
    - Else → fall back to DefaultAzureCredential (original behavior)
    """
    if MANUAL_BEARER_TOKEN:
        return MANUAL_BEARER_TOKEN.strip()
    
    # Fallback to Azure auth (only if token not provided)
    # try:
    #     from azure.identity import DefaultAzureCredential
    #     return DefaultAzureCredential().get_token("https://ai.azure.com").token
    # except Exception as e:
    #     raise RuntimeError("Failed to get token via DefaultAzureCredential") from e


def call_api(method: str, url: str, json_body: dict | None = None, token: Optional[str] = None):
    """
    Call Azure AI Studio API.
    You can now override the token per-call.
    """
    auth_token = token or get_token()  # Use passed token or get from env/auth

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json"
    }
    resp = requests.request(method, url, headers=headers, json=json_body)
    resp.raise_for_status()
    return resp.json()

# ------------------------------------------------------------------
# 3. JSON-RPC error helper
# ------------------------------------------------------------------
def jsonrpc_error(code: int, message: str, req_id=None):
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}

# ------------------------------------------------------------------
# 4. GET  /.well-known/agent-card.json
# ------------------------------------------------------------------
@app.get("/api/chat/.well-known/agent-card.json")
def get_agent_card():
    card = {
        "name": "Capital Agent",
        "description": "Answers capital-city questions for any country",
        "version": "1.0.0",
        "url": "http://127.0.0.1:8000/api/chat",
        "preferredTransport": "JSONRPC",
        "protocolVersion": "0.3.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "capital_query",
                "name": "capital_query",
                "description": "Answers capital-city questions",
                "inputModes": ["text"],
                "outputModes": ["text"],
                "tags": ["capital", "country"]
            }
        ]
    }
    return JSONResponse(content=card, headers={"Access-Control-Allow-Origin": "*"})

# ------------------------------------------------------------------
# 5. POST /api/chat  –  A2A message/send
# ------------------------------------------------------------------
class Part(BaseModel):
    kind: str
    text: str | None = None

class MessageIn(BaseModel):
    role: str
    parts: list[Part]
    messageId: str
    contextId: str | None = None

class ParamsIn(BaseModel):
    message: MessageIn

class JsonRpcIn(BaseModel):
    jsonrpc: str
    method: str
    params: ParamsIn
    id: str

@app.post("/api/chat")
async def chat(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            content=jsonrpc_error(-32700, "Parse error"),
            status_code=200,
            headers={"Access-Control-Allow-Origin": "*"}
        )

    # Basic JSON-RPC validation
    if payload.get("jsonrpc") != "2.0":
        return JSONResponse(content=jsonrpc_error(-32600, "Invalid JSON-RPC version", payload.get("id")),
                            status_code=200, headers={"Access-Control-Allow-Origin": "*"})
    if payload.get("method") not in ["message/send", "sendMessage"]:
        return JSONResponse(content=jsonrpc_error(-32601, f"Method not found: {payload.get('method')}", payload.get("id")),
                            status_code=200, headers={"Access-Control-Allow-Origin": "*"})

    req_id = payload.get("id")
    message = payload.get("params", {}).get("message", {})

    # Extract user text
    user_text = ""
    for part in message.get("parts", []):
        if part.get("kind") == "text" and part.get("text"):
            user_text = part["text"]
            break

    if not user_text:
        return JSONResponse(content=jsonrpc_error(-32602, "No valid text part", req_id),
                            status_code=200, headers={"Access-Control-Allow-Origin": "*"})

    # ------------------------------------------------------------------
    # Call Azure AI Studio (same logic as your function)
    # ------------------------------------------------------------------
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
            if status == "completed":
                break
            if status in ["failed", "cancelled"]:
                raise RuntimeError(f"Run {status}")
            time.sleep(1.5)

        messages = call_api("GET", f"{BASE_URL}/threads/{thread_id}/messages?api-version={API_VERSION}")
        reply_msg = next((m for m in messages["data"] if m["role"] == "assistant"), None)
        reply_text = reply_msg["content"][0]["text"]["value"] if reply_msg else "No reply"

    except Exception as e:
        logging.exception("AI call failed")
        return JSONResponse(content=jsonrpc_error(-32000, str(e), req_id),
                            status_code=200, headers={"Access-Control-Allow-Origin": "*"})

    # ------------------------------------------------------------------
    # Return A2A-compliant Task (NO extra "result" wrapper)
    # ------------------------------------------------------------------
    task_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    context_id = message.get("contextId") or str(uuid.uuid4())

    task = {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": "completed",
        "messages": [
            {
                "role": "assistant",
                "parts": [{"kind": "text", "text": reply_text}],
                "messageId": message_id
            }
        ]
    }

    response = {
        "jsonrpc": "2.0",
        "result": task,
        "id": req_id
    }

    return JSONResponse(
        content=response,
        headers={"Access-Control-Allow-Origin": "*"}
    )