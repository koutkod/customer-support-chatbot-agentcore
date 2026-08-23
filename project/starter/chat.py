#!/usr/bin/env python3
"""
chat.py

A minimal terminal chat client for the customer support chatbot harness.
Each run of this script is one fresh multi-turn conversation: a new
runtimeSessionId is generated at startup, and the harness (not this script)
keeps track of the conversation across turns.

Usage:
    python chat.py

Type your message and press Enter. Type `exit` or `quit` (or Ctrl+D) to end
the session. Tool calls the model makes are printed as
`[tool call] <tool name>(<args>)`, e.g.:

    [tool call] bugreports___create_bug_report({"description": "...", ...})

if you never see that line during a bug report conversation, your system
prompt probably isn't telling the model clearly when to call the tool.
"""

import json
import sys
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
HARNESS_CONFIG_FILE = "harness_config.json"


def load_harness_arn():
    try:
        with open(HARNESS_CONFIG_FILE) as f:
            return json.load(f)["harness_arn"]
    except FileNotFoundError:
        print(f"{HARNESS_CONFIG_FILE} not found. Run create_harness.py first.")
        sys.exit(1)
    except KeyError:
        print(f"{HARNESS_CONFIG_FILE} is missing 'harness_arn'. Re-run create_harness.py.")
        sys.exit(1)


class StreamPrinter:
    """Tracks per-content-block state while iterating an invoke_harness event stream."""

    def __init__(self):
        self.blocks = {}  # contentBlockIndex -> {"kind": ..., "text": "", "name": ..., "input": ""}

    def handle_event(self, event):
        if "contentBlockStart" in event:
            idx = event["contentBlockStart"]["contentBlockIndex"]
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                name = start["toolUse"].get("name", "?")
                self.blocks[idx] = {"kind": "toolUse", "name": name, "input": ""}
            elif "toolResult" in start:
                self.blocks[idx] = {"kind": "toolResult", "text": ""}
            else:
                self.blocks[idx] = {"kind": "text"}

        elif "contentBlockDelta" in event:
            idx = event["contentBlockDelta"]["contentBlockIndex"]
            delta = event["contentBlockDelta"].get("delta", {})
            block = self.blocks.setdefault(idx, {"kind": "text"})
            if "text" in delta:
                print(delta["text"], end="", flush=True)
            elif "toolUse" in delta:
                block["input"] += delta["toolUse"].get("input", "")
            elif "toolResult" in delta:
                for item in delta["toolResult"]:
                    if "text" in item:
                        block.setdefault("text", "")
                        block["text"] += item["text"]
                    elif "json" in item:
                        block.setdefault("text", "")
                        block["text"] += json.dumps(item["json"])

        elif "contentBlockStop" in event:
            idx = event["contentBlockStop"]["contentBlockIndex"]
            block = self.blocks.get(idx, {})
            if block.get("kind") == "toolUse":
                args = block.get("input", "")
                try:
                    args = json.dumps(json.loads(args)) if args else "{}"
                except json.JSONDecodeError:
                    pass
                print(f"\n[tool call] {block['name']}({args})")
            elif block.get("kind") == "toolResult":
                text = block.get("text", "")
                if text:
                    print(f"[tool result] {text}")

        elif "validationException" in event:
            print(f"\n[error] {event['validationException'].get('message')}")

        elif "internalServerException" in event:
            print(f"\n[error] {event['internalServerException'].get('message')}")

        elif "runtimeClientError" in event:
            print(f"\n[error] {event['runtimeClientError'].get('message')}")


def main():
    harness_arn = load_harness_arn()
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    session_id = str(uuid.uuid4())

    print("Connected to the customer support chatbot. Type 'exit' to quit.")
    print(f"(session: {session_id})\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        try:
            response = client.invoke_harness(
                harnessArn=harness_arn,
                runtimeSessionId=session_id,
                messages=[{"role": "user", "content": [{"text": user_input}]}],
            )
        except ClientError as e:
            print(f"[error] invoke_harness failed: {e}")
            continue

        print("Bot: ", end="", flush=True)
        printer = StreamPrinter()
        for event in response["stream"]:
            printer.handle_event(event)
        print("\n")


if __name__ == "__main__":
    main()
