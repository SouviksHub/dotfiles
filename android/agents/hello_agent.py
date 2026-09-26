#!/usr/bin/env python3
"""Minimal tool-calling agent against the local llama-server.

Stdlib only: pip wheels with native code (pydantic-core, numpy, ...) often
need a Rust/C toolchain on Termux, so the reference loop avoids them.

    agent-run python ~/agents/hello_agent.py "How much battery is left?"
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = os.environ["OPENAI_BASE_URL"].rstrip("/")
KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.environ.get("OPENAI_MODEL", "local")
WORKSPACE = (Path.home() / "agents" / "workspace").resolve()
MAX_STEPS = 6


def _inside_workspace(path: str) -> Path:
    p = (WORKSPACE / path).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ValueError("path escapes workspace")
    return p


def battery() -> str:
    out = subprocess.run(["termux-battery-status"], capture_output=True,
                         text=True, timeout=10)
    return out.stdout or out.stderr


def list_dir(path: str = ".") -> str:
    return "\n".join(sorted(e.name for e in _inside_workspace(path).iterdir()))


def read_file(path: str) -> str:
    return _inside_workspace(path).read_text(errors="replace")[:8000]


def write_file(path: str, content: str) -> str:
    p = _inside_workspace(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} bytes to {path}"


TOOLS = {f.__name__: f for f in (battery, list_dir, read_file, write_file)}
SCHEMAS = [
    {"type": "function", "function": {
        "name": "battery", "description": "Phone battery level and temperature.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "list_dir", "description": "List files in the agent workspace.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a file from the agent workspace.",
        "parameters": {"type": "object", "required": ["path"],
                       "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write a file in the agent workspace.",
        "parameters": {"type": "object", "required": ["path", "content"],
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}}}}},
]


def chat(messages):
    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps({"model": MODEL, "messages": messages,
                         "tools": SCHEMAS, "temperature": 0.2}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["choices"][0]["message"]


def main():
    task = " ".join(sys.argv[1:]) or "List the files in my workspace."
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    messages = [
        {"role": "system", "content": "You are an agent running on an Android "
         "phone. Use tools when they help. Be brief."},
        {"role": "user", "content": task},
    ]
    for _ in range(MAX_STEPS):
        msg = chat(messages)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            print(msg.get("content", "").strip())
            return
        for call in calls:
            name = call["function"]["name"]
            args = {}
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                result = TOOLS[name](**args)
            except Exception as e:  # report tool errors back to the model
                result = f"error: {e}"
            print(f"[tool] {name}({args}) -> "
                  f"{str(result)[:120]!r}", file=sys.stderr)
            messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                             "content": str(result)})
    print("step limit reached", file=sys.stderr)


if __name__ == "__main__":
    main()
