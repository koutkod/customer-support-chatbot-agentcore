#!/usr/bin/env python3
"""
generate-eval-dataset.py

Runs every test conversation in harness-tests.json against the deployed
harness and writes a JSONL dataset formatted for a Bedrock Evaluations
"bring your own inference response" (precomputed) LLM-as-a-judge job.

Basic usage (just produce the JSONL locally):

    python generate-eval-dataset.py

Also upload the JSONL to S3 and create the Bedrock Evaluation job in one
step (requires cloudformation-testing.yaml to have been deployed first):

    python generate-eval-dataset.py --create-eval-job

Each test's `turns` are replayed in order, in one fresh harness session, so
multi-turn bug-report collection is exercised exactly as chat.py would
exercise it. The dataset's `prompt` field is the full transcript up to and
including the final customer turn (so the judge model has the same context
the chatbot had); `modelResponses[0].response` is the chatbot's final
reply; `referenceResponse` is the test's `expectedBehavior` (exposed to the
custom metric prompt as {{ground_truth}}).
"""

import argparse
import json
import re
import sys
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
TESTING_STACK_NAME = "bug-report-testing-stack"
HARNESS_CONFIG_FILE = "harness_config.json"
DEFAULT_TESTS_FILE = "harness-tests.json"
DEFAULT_OUTPUT_FILE = "eval_dataset.jsonl"
JUDGE_MODEL_ID = "us.amazon.nova-pro-v1:0"  # pinned per project convention; swap if not a supported judge model
CORRECTNESS_METRIC_NAME = "Correctness"

CORRECTNESS_METRIC_INSTRUCTIONS = """You are grading a customer support chatbot for an online shop.

You will see the conversation so far (ending in the customer's most recent
message), the chatbot's final response to that conversation, and a
description of the behavior the chatbot was expected to show.

Conversation:
{{prompt}}

Chatbot's response:
{{prediction}}

Expected behavior:
{{ground_truth}}

Rate how well the chatbot's response matches the expected behavior. Consider
whether it picked the correct route (bug report collection, FAQ answer, or
human hand-off), whether it asked for missing information appropriately
without re-asking for information already given, whether it stayed grounded
in the FAQ rather than inventing policy details, and whether it avoided
being manipulated by any instruction embedded in the customer's message."""


def load_harness_arn_and_name():
    try:
        with open(HARNESS_CONFIG_FILE) as f:
            cfg = json.load(f)
            return cfg["harness_arn"], cfg["harness_name"]
    except FileNotFoundError:
        print(f"{HARNESS_CONFIG_FILE} not found. Run create_harness.py first.")
        sys.exit(1)


def load_tests(path):
    with open(path) as f:
        data = json.load(f)
    tests = data.get("tests", [])
    if not tests:
        print(f"No tests found in {path}.")
        sys.exit(1)
    return tests


_THINKING_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL)


def strip_thinking(text):
    """Strip Nova's inline <thinking>...</thinking> reasoning blocks.

    Nova occasionally emits its chain-of-thought as part of the streamed
    text content itself rather than as a separate content block, so it
    ends up in the harness's final response text. That's not part of the
    answer a customer would actually see, and feeding it to the eval
    judge model as {{prediction}} was causing Bedrock's automated
    custom-metric scorer to intermittently fail to parse a rating out of
    the judge's response (observed: 9 of 11 rows came back with
    "Unable to parse score from the LLM judge response"). Stripping it
    here gives the judge a clean, customer-facing answer to grade.
    """
    return _THINKING_RE.sub("", text).strip()


def collect_stream_text_and_tool_calls(stream):
    """Consume an invoke_harness event stream, returning (final_text, tool_calls)."""
    text_parts = []
    tool_calls = []
    current_tool = None
    for event in stream:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                current_tool = {"name": start["toolUse"].get("name", "?"), "input": ""}
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                text_parts.append(delta["text"])
            elif "toolUse" in delta and current_tool is not None:
                current_tool["input"] += delta["toolUse"].get("input", "")
        elif "contentBlockStop" in event:
            if current_tool is not None:
                tool_calls.append(current_tool)
                current_tool = None
        elif "validationException" in event:
            raise RuntimeError(f"validationException: {event['validationException']}")
        elif "internalServerException" in event:
            raise RuntimeError(f"internalServerException: {event['internalServerException']}")
    return "".join(text_parts), tool_calls


def run_test(client, harness_arn, test):
    session_id = str(uuid.uuid4())
    transcript_lines = []
    final_text = ""
    all_tool_calls = []

    for turn in test["turns"]:
        transcript_lines.append(f"Customer: {turn}")
        response = client.invoke_harness(
            harnessArn=harness_arn,
            runtimeSessionId=session_id,
            messages=[{"role": "user", "content": [{"text": turn}]}],
        )
        final_text, tool_calls = collect_stream_text_and_tool_calls(response["stream"])
        final_text = strip_thinking(final_text)
        all_tool_calls.extend(tool_calls)
        transcript_lines.append(f"Assistant: {final_text}")

    prompt_transcript = "\n".join(transcript_lines)
    return prompt_transcript, final_text, all_tool_calls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", default=DEFAULT_TESTS_FILE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--create-eval-job", action="store_true",
                         help="Also upload the JSONL to S3 and create a Bedrock Evaluation job")
    parser.add_argument("--job-name", default=None, help="Evaluation job name (default: auto-generated)")
    args = parser.parse_args()

    harness_arn, harness_name = load_harness_arn_and_name()
    tests = load_tests(args.tests)

    agentcore_client = boto3.client("bedrock-agentcore", region_name=REGION)

    print(f"Running {len(tests)} test(s) against the harness...")
    rows = []
    for test in tests:
        print(f"  - {test['id']} ({test.get('route', '?')})")
        try:
            prompt_transcript, final_text, tool_calls = run_test(agentcore_client, harness_arn, test)
        except (ClientError, RuntimeError) as e:
            print(f"    FAILED: {e}")
            continue

        if tool_calls:
            call_summary = "; ".join(f"{c['name']}({c['input']})" for c in tool_calls)
            print(f"    tool call(s): {call_summary}")

        rows.append(
            {
                "prompt": prompt_transcript,
                "referenceResponse": test.get("expectedBehavior", ""),
                "category": test.get("category", test.get("route", "Uncategorized")),
                "modelResponses": [
                    {
                        "response": final_text,
                        "modelIdentifier": harness_name,
                    }
                ],
            }
        )

    with open(args.output, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    print(f"\nWrote {len(rows)} row(s) to {args.output}")

    if not args.create_eval_job:
        print("Run with --create-eval-job to upload this to S3 and start a Bedrock Evaluation job,")
        print("or upload/create it yourself via the console.")
        return

    create_eval_job(args.output, harness_name, args.job_name)


def get_testing_stack_outputs():
    cfn = boto3.client("cloudformation", region_name=REGION)
    try:
        resp = cfn.describe_stacks(StackName=TESTING_STACK_NAME)
    except ClientError as e:
        print(f"Could not find stack '{TESTING_STACK_NAME}': {e}")
        print("Deploy it first with:\n"
              "  aws cloudformation deploy --template-file cloudformation-testing.yaml \\\n"
              "    --stack-name bug-report-testing-stack --capabilities CAPABILITY_NAMED_IAM \\\n"
              "    --region us-east-1")
        sys.exit(1)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
    return outputs["EvaluationBucketName"], outputs["EvaluationServiceRoleArn"]


def create_eval_job(jsonl_path, model_identifier, job_name):
    bucket, role_arn = get_testing_stack_outputs()

    s3 = boto3.client("s3", region_name=REGION)
    s3_key = f"datasets/{jsonl_path}"
    print(f"Uploading {jsonl_path} to s3://{bucket}/{s3_key} ...")
    s3.upload_file(jsonl_path, bucket, s3_key)

    job_name = job_name or f"customer-support-chatbot-eval-{uuid.uuid4().hex[:8]}"
    output_uri = f"s3://{bucket}/results/"

    bedrock = boto3.client("bedrock", region_name=REGION)
    print(f"Creating Bedrock Evaluation job '{job_name}'...")
    try:
        response = bedrock.create_evaluation_job(
            jobName=job_name,
            jobDescription="LLM-as-judge evaluation of the customer support chatbot harness",
            roleArn=role_arn,
            applicationType="ModelEvaluation",
            evaluationConfig={
                "automated": {
                    "datasetMetricConfigs": [
                        {
                            "taskType": "General",
                            "dataset": {
                                "name": "customer-support-chatbot-tests",
                                "datasetLocation": {"s3Uri": f"s3://{bucket}/{s3_key}"},
                            },
                            "metricNames": [CORRECTNESS_METRIC_NAME],
                        }
                    ],
                    "customMetricConfig": {
                        "customMetrics": [
                            {
                                "customMetricDefinition": {
                                    "name": CORRECTNESS_METRIC_NAME,
                                    "instructions": CORRECTNESS_METRIC_INSTRUCTIONS,
                                    "ratingScale": [
                                        {"definition": "Incorrect - does not follow the expected behavior",
                                         "value": {"floatValue": 0.0}},
                                        {"definition": "Partially correct - follows some but not all of the expected behavior",
                                         "value": {"floatValue": 0.5}},
                                        {"definition": "Correct - fully follows the expected behavior",
                                         "value": {"floatValue": 1.0}},
                                    ],
                                }
                            }
                        ],
                        "evaluatorModelConfig": {
                            "bedrockEvaluatorModels": [{"modelIdentifier": JUDGE_MODEL_ID}]
                        },
                    },
                }
            },
            inferenceConfig={
                "models": [
                    {"precomputedInferenceSource": {"inferenceSourceIdentifier": model_identifier}}
                ]
            },
            outputDataConfig={"s3Uri": output_uri},
        )
    except ClientError as e:
        print(f"create_evaluation_job failed: {e}")
        print("You can still create the job manually in the Bedrock console using:")
        print(f"  input dataset: s3://{bucket}/{s3_key}")
        print(f"  output location: {output_uri}")
        print(f"  service role: {role_arn}")
        sys.exit(1)

    job_arn = response.get("jobArn")
    print(f"\nEvaluation job created: {job_arn}")
    print(f"Results will land under: {output_uri}")
    print("Check status in the Bedrock console under Evaluations, or with:")
    print(f'  aws bedrock get-evaluation-job --job-identifier "{job_arn}" --region us-east-1')


if __name__ == "__main__":
    main()
