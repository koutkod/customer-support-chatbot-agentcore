#!/usr/bin/env python3
"""
setup_gateway.py

Creates the AgentCore Gateway and registers the create_bug_report Lambda
(from cloudformation-tool.yaml) as a Gateway target so the model can call it
as the tool `bugreports___create_bug_report`.

Reads everything it needs from the bug-report-tool-stack CloudFormation
stack outputs -- no copy-pasting ARNs required. Run it after the stack has
finished deploying:

    aws cloudformation deploy --template-file cloudformation-tool.yaml \\
      --stack-name bug-report-tool-stack --capabilities CAPABILITY_NAMED_IAM \\
      --region us-east-1
    python setup_gateway.py

It is safe to re-run: if a gateway/target with the same name already
exists it is reused instead of re-created.

Saves gateway/target identifiers to agentcore_config.json, which
create_harness.py, chat.py, and cleanup_agentcore.py all read.
"""

import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
STACK_NAME = "bug-report-tool-stack"
GATEWAY_NAME = "customer-support-gateway"  # must match GatewayNamePrefix in cloudformation-tool.yaml
TARGET_NAME = "bugreports"  # letters/digits/underscores only -- becomes the bugreports___create_bug_report tool prefix
CONFIG_FILE = "agentcore_config.json"

RETRY_ATTEMPTS = 6
RETRY_DELAY_SECONDS = 15  # IAM propagation after the stack finishes can take ~a minute


def get_stack_outputs(stack_name):
    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
    except ClientError as e:
        print(f"Could not find stack '{stack_name}': {e}")
        print("Deploy it first with:\n"
              "  aws cloudformation deploy --template-file cloudformation-tool.yaml \\\n"
              "    --stack-name bug-report-tool-stack --capabilities CAPABILITY_NAMED_IAM \\\n"
              "    --region us-east-1")
        sys.exit(1)

    stacks = resp["Stacks"]
    if not stacks:
        print(f"Stack '{stack_name}' has no entries.")
        sys.exit(1)

    status = stacks[0]["StackStatus"]
    if not status.endswith("COMPLETE"):
        print(f"Stack '{stack_name}' is in status {status}, not *_COMPLETE. Wait for it to finish and re-run.")
        sys.exit(1)

    outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    required = ["CreateBugReportFunctionArn", "GatewayRoleArn", "HarnessExecutionRoleArn", "BugReportsTableName"]
    missing = [k for k in required if k not in outputs]
    if missing:
        print(f"Stack outputs missing: {missing}. Did cloudformation-tool.yaml change without updating this script?")
        sys.exit(1)

    return outputs


def with_retries(fn, description):
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            last_err = e
            if code in ("AccessDeniedException", "ValidationException") and attempt < RETRY_ATTEMPTS:
                print(f"[{attempt}/{RETRY_ATTEMPTS}] {description} failed with {code} "
                      f"(likely IAM propagation delay). Retrying in {RETRY_DELAY_SECONDS}s...")
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            raise
    raise last_err


def find_existing_gateway(client, name):
    paginator = client.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gw in page.get("items", page.get("gateways", [])):
            if gw.get("name") == name:
                return gw
    return None


def find_existing_target(client, gateway_id, name):
    paginator = client.get_paginator("list_gateway_targets")
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        for target in page.get("items", page.get("targets", [])):
            if target.get("name") == name:
                return target
    return None


def wait_for_status(get_fn, ok_statuses, fail_statuses, label, timeout_seconds=180, poll_seconds=5):
    start = time.time()
    while True:
        resp = get_fn()
        status = resp.get("status")
        if status in ok_statuses:
            return resp
        if status in fail_statuses:
            raise RuntimeError(f"{label} ended in status {status}: {resp.get('statusReasons')}")
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"Timed out waiting for {label} (last status: {status})")
        print(f"  {label} status: {status} -- waiting...")
        time.sleep(poll_seconds)


def main():
    print(f"Reading outputs from stack '{STACK_NAME}'...")
    outputs = get_stack_outputs(STACK_NAME)
    lambda_arn = outputs["CreateBugReportFunctionArn"]
    gateway_role_arn = outputs["GatewayRoleArn"]
    print(f"  Lambda ARN:       {lambda_arn}")
    print(f"  Gateway role ARN: {gateway_role_arn}")

    client = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # --- Gateway -----------------------------------------------------
    existing_gateway = find_existing_gateway(client, GATEWAY_NAME)
    if existing_gateway:
        gateway_id = existing_gateway.get("gatewayId") or existing_gateway.get("gatewayIdentifier")
        print(f"Gateway '{GATEWAY_NAME}' already exists (id={gateway_id}). Reusing it.")
        gateway = client.get_gateway(gatewayIdentifier=gateway_id)
    else:
        print(f"Creating gateway '{GATEWAY_NAME}'...")
        create_resp = with_retries(
            lambda: client.create_gateway(
                name=GATEWAY_NAME,
                description="Gateway exposing the create_bug_report tool to the customer support chatbot harness",
                roleArn=gateway_role_arn,
                protocolType="MCP",
                authorizerType="AWS_IAM",
            ),
            "create_gateway",
        )
        gateway_id = create_resp.get("gatewayId") or create_resp.get("gatewayIdentifier")
        gateway = wait_for_status(
            lambda: client.get_gateway(gatewayIdentifier=gateway_id),
            ok_statuses={"READY"},
            fail_statuses={"FAILED"},
            label="Gateway",
        )

    gateway_arn = gateway.get("gatewayArn") or gateway.get("arn")
    gateway_url = gateway.get("gatewayUrl")
    print(f"Gateway ready: {gateway_arn}")

    # --- Gateway target (the create_bug_report tool) ------------------
    tool_schema = {
        "inlinePayload": [
            {
                "name": "create_bug_report",
                "description": (
                    "File a bug report ticket for the customer. Only call this once you have "
                    "collected all three required fields from the conversation."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "Description of the bug, in the customer's own words.",
                        },
                        "stepsToReproduce": {
                            "type": "string",
                            "description": "Step-by-step instructions to reproduce the issue.",
                        },
                        "environment": {
                            "type": "string",
                            "description": "The customer's environment: browser/app, OS, and device.",
                        },
                    },
                    "required": ["description", "stepsToReproduce", "environment"],
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "ticketId": {"type": "string"},
                        "status": {"type": "string"},
                    },
                },
            }
        ]
    }

    existing_target = find_existing_target(client, gateway_id, TARGET_NAME)
    if existing_target:
        target_id = existing_target.get("targetId")
        print(f"Gateway target '{TARGET_NAME}' already exists (id={target_id}). Reusing it.")
    else:
        print(f"Creating gateway target '{TARGET_NAME}'...")
        target_resp = with_retries(
            lambda: client.create_gateway_target(
                gatewayIdentifier=gateway_id,
                name=TARGET_NAME,
                description="create_bug_report Lambda tool",
                targetConfiguration={"mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": tool_schema}}},
                credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
            ),
            "create_gateway_target",
        )
        target_id = target_resp.get("targetId")
        wait_for_status(
            lambda: client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id),
            ok_statuses={"READY"},
            fail_statuses={"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"},
            label="Gateway target",
        )

    print(f"Gateway target ready. Tool exposed to the model as: {TARGET_NAME}___create_bug_report")

    config = {
        "region": REGION,
        "stack_name": STACK_NAME,
        "gateway_name": GATEWAY_NAME,
        "gateway_id": gateway_id,
        "gateway_arn": gateway_arn,
        "gateway_url": gateway_url,
        "target_name": TARGET_NAME,
        "target_id": target_id,
        "tool_name": f"{TARGET_NAME}___create_bug_report",
        "harness_execution_role_arn": outputs["HarnessExecutionRoleArn"],
        "bug_reports_table_name": outputs["BugReportsTableName"],
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\nSaved gateway configuration to {CONFIG_FILE}")
    print("Next: write system_prompt.txt, then run create_harness.py")


if __name__ == "__main__":
    main()
