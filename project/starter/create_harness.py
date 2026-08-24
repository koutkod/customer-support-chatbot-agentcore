#!/usr/bin/env python3
"""
create_harness.py

Creates (or updates, if it already exists) the AgentCore managed harness for
the customer support chatbot, from system_prompt.txt.

- Reads system_prompt.txt and replaces the literal `{{FAQ}}` placeholder
  with the full contents of online_shop_faq.md.
- Pins the model to us.amazon.nova-pro-v1:0 (a cross-region inference
  profile) everywhere -- do not rely on the harness default model, which
  requires an AWS Marketplace subscription that lab accounts can't
  complete.
- Wires up the AgentCore Gateway created by setup_gateway.py as a tool, so
  the model can call bugreports___create_bug_report.

Run this every time you change system_prompt.txt or online_shop_faq.md:

    python create_harness.py     # first run takes ~2-3 minutes

There is no separate "prepare"/deploy step -- as soon as this script
finishes, chat.py talks to the updated prompt.
"""

import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
MODEL_ID = "us.amazon.nova-pro-v1:0"  # pinned everywhere in this project -- do not change to the harness default
HARNESS_NAME = "customer_support_chatbot"
SYSTEM_PROMPT_FILE = "system_prompt.txt"
FAQ_FILE = "online_shop_faq.md"
GATEWAY_CONFIG_FILE = "agentcore_config.json"
HARNESS_CONFIG_FILE = "harness_config.json"

FAQ_PLACEHOLDER = "{{FAQ}}"


def load_gateway_config():
    try:
        with open(GATEWAY_CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"{GATEWAY_CONFIG_FILE} not found. Run setup_gateway.py first.")
        sys.exit(1)


def build_system_prompt():
    with open(SYSTEM_PROMPT_FILE) as f:
        prompt = f.read()
    if FAQ_PLACEHOLDER not in prompt:
        print(f"Warning: {SYSTEM_PROMPT_FILE} does not contain the {FAQ_PLACEHOLDER} placeholder; "
              "the FAQ will not be embedded.")
        return prompt
    with open(FAQ_FILE) as f:
        faq = f.read()
    return prompt.replace(FAQ_PLACEHOLDER, faq)


def find_existing_harness(client, name):
    paginator = client.get_paginator("list_harnesses")
    for page in paginator.paginate():
        for h in page.get("items", page.get("harnessSummaries", [])):
            if h.get("harnessName") == name:
                return h
    return None


def wait_for_status(get_fn, ok_statuses, fail_statuses, label, timeout_seconds=240, poll_seconds=5):
    start = time.time()
    while True:
        resp = get_fn()["harness"]
        status = resp.get("status")
        if status in ok_statuses:
            return resp
        if status in fail_statuses:
            raise RuntimeError(f"{label} ended in status {status}")
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"Timed out waiting for {label} (last status: {status})")
        print(f"  {label} status: {status} -- waiting...")
        time.sleep(poll_seconds)


def main():
    gw_config = load_gateway_config()
    system_prompt_text = build_system_prompt()

    tools = [
        {
            "type": "agentcore_gateway",
            "name": gw_config["target_name"],
            "config": {
                "agentCoreGateway": {
                    "gatewayArn": gw_config["gateway_arn"],
                    "outboundAuth": {"awsIam": {}},
                }
            },
        }
    ]

    model = {
        "bedrockModelConfig": {
            "modelId": MODEL_ID,
            "temperature": 0.2,
        }
    }

    client = boto3.client("bedrock-agentcore-control", region_name=REGION)

    existing = find_existing_harness(client, HARNESS_NAME)
    try:
        if existing:
            harness_id = existing.get("harnessId")
            print(f"Harness '{HARNESS_NAME}' already exists (id={harness_id}). Updating it...")
            client.update_harness(
                harnessIdentifier=harness_id,
                executionRoleArn=gw_config["harness_execution_role_arn"],
                model=model,
                systemPrompt=[{"text": system_prompt_text}],
                tools=tools,
            )
        else:
            print(f"Creating harness '{HARNESS_NAME}' (first run takes ~2-3 minutes)...")
            create_resp = client.create_harness(
                harnessName=HARNESS_NAME,
                executionRoleArn=gw_config["harness_execution_role_arn"],
                model=model,
                systemPrompt=[{"text": system_prompt_text}],
                tools=tools,
                maxIterations=8,
            )
            harness_id = create_resp["harness"]["harnessId"]
    except ClientError as e:
        print(f"AgentCore rejected the request: {e}")
        print("If this mentions the execution role, it may be IAM propagation delay right after "
              "the CloudFormation stack finished -- wait a minute and re-run.")
        sys.exit(1)

    harness = wait_for_status(
        lambda: client.get_harness(harnessIdentifier=harness_id),
        ok_statuses={"READY"},
        fail_statuses={"CREATE_FAILED", "UPDATE_FAILED"},
        label="Harness",
    )

    harness_arn = harness["arn"]
    print(f"\nHarness ready: {harness_arn}")

    with open(HARNESS_CONFIG_FILE, "w") as f:
        json.dump(
            {
                "region": REGION,
                "harness_name": HARNESS_NAME,
                "harness_id": harness_id,
                "harness_arn": harness_arn,
                "model_id": MODEL_ID,
            },
            f,
            indent=2,
        )

    print(f"Saved harness configuration to {HARNESS_CONFIG_FILE}")
    print("Next: python chat.py")


if __name__ == "__main__":
    main()
