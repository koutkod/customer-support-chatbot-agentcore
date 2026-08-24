#!/usr/bin/env python3
"""
cleanup_agentcore.py

Deletes the AgentCore resources created by create_harness.py and
setup_gateway.py: the harness, the gateway target, and the gateway --
in that order, since the gateway can't be deleted while it still has a
target. Does NOT delete the CloudFormation stacks (the DynamoDB table,
Lambda, and IAM roles) or local config files; run

    aws cloudformation delete-stack --stack-name bug-report-tool-stack --region us-east-1
    aws cloudformation delete-stack --stack-name bug-report-testing-stack --region us-east-1

separately if you want those gone too.

Safe to re-run: skips anything that's already gone.
"""

import json
import os

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
HARNESS_CONFIG_FILE = "harness_config.json"
GATEWAY_CONFIG_FILE = "agentcore_config.json"


def delete_harness(client):
    if not os.path.exists(HARNESS_CONFIG_FILE):
        print(f"No {HARNESS_CONFIG_FILE} found; skipping harness deletion.")
        return
    with open(HARNESS_CONFIG_FILE) as f:
        cfg = json.load(f)
    harness_id = cfg.get("harness_id")
    if not harness_id:
        print("No harness_id in config; skipping.")
        return
    try:
        print(f"Deleting harness {harness_id}...")
        client.delete_harness(harnessIdentifier=harness_id)
        print("Harness deletion requested.")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            print("Harness already gone.")
        else:
            print(f"Could not delete harness: {e}")


def delete_gateway_target_and_gateway(client):
    if not os.path.exists(GATEWAY_CONFIG_FILE):
        print(f"No {GATEWAY_CONFIG_FILE} found; skipping gateway/target deletion.")
        return
    with open(GATEWAY_CONFIG_FILE) as f:
        cfg = json.load(f)

    gateway_id = cfg.get("gateway_id")
    target_id = cfg.get("target_id")

    if gateway_id and target_id:
        try:
            print(f"Deleting gateway target {target_id}...")
            client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
            print("Gateway target deletion requested.")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                print("Gateway target already gone.")
            else:
                print(f"Could not delete gateway target: {e}")

    if gateway_id:
        try:
            print(f"Deleting gateway {gateway_id}...")
            client.delete_gateway(gatewayIdentifier=gateway_id)
            print("Gateway deletion requested.")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                print("Gateway already gone.")
            elif code == "ConflictException":
                print("Gateway still has a target attached; wait a moment for the target "
                      "deletion to finish and re-run this script.")
            else:
                print(f"Could not delete gateway: {e}")


def main():
    control_client = boto3.client("bedrock-agentcore-control", region_name=REGION)

    # Delete the harness first (it references the gateway), then the target,
    # then the gateway itself.
    delete_harness(control_client)
    delete_gateway_target_and_gateway(control_client)

    print("\nDone. This did not touch the CloudFormation stacks (DynamoDB table, Lambda, "
          "IAM roles) or the evaluation S3 bucket -- delete those with:")
    print("  aws cloudformation delete-stack --stack-name bug-report-tool-stack --region us-east-1")
    print("  aws cloudformation delete-stack --stack-name bug-report-testing-stack --region us-east-1")


if __name__ == "__main__":
    main()
