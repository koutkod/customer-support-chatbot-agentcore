"""
create_bug_report.py

Lambda implementation of the `create_bug_report` tool.

This function is invoked directly by the AgentCore Gateway on behalf of the
model. The Gateway sends the tool's arguments straight through as the Lambda
event -- a plain JSON object with no wrapper envelope (Bedrock Agents Classic
used to wrap tool input in a messageVersion/parameters structure; the
AgentCore Gateway does not).

Expected event shape (all three fields are required):
{
    "description": "The checkout page crashes when I click the Pay button",
    "stepsToReproduce": "1. Add an item to the cart. 2. Go to checkout. 3. Click Pay.",
    "environment": "Chrome 120 on macOS Sonoma"
}

The function writes one item to DynamoDB (table name from the TABLE_NAME
environment variable) and returns the created ticket, including a freshly
generated ticketId and a status of "OPEN".
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")

REQUIRED_FIELDS = ("description", "stepsToReproduce", "environment")


class MissingFieldError(Exception):
    """Raised when the incoming event is missing a required field."""


def _validate(event):
    if not isinstance(event, dict):
        raise MissingFieldError(
            f"Expected a JSON object with {REQUIRED_FIELDS}, got: {type(event).__name__}"
        )

    missing = [
        field
        for field in REQUIRED_FIELDS
        if not isinstance(event.get(field), str) or not event.get(field).strip()
    ]
    if missing:
        raise MissingFieldError(
            f"Missing or empty required field(s): {', '.join(missing)}. "
            f"All of {REQUIRED_FIELDS} are required."
        )


def handler(event, context):
    # Ground truth for what actually reached the Lambda -- check CloudWatch
    # Logs (/aws/lambda/<this-function-name>) when troubleshooting.
    logger.info("Received event: %s", json.dumps(event))

    table_name = os.environ.get("TABLE_NAME")
    if not table_name:
        raise RuntimeError("TABLE_NAME environment variable is not set")

    _validate(event)

    ticket_id = f"BUG-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()

    item = {
        "ticketId": ticket_id,
        "description": event["description"].strip(),
        "stepsToReproduce": event["stepsToReproduce"].strip(),
        "environment": event["environment"].strip(),
        "status": "OPEN",
        "createdAt": now,
    }

    table = dynamodb.Table(table_name)
    table.put_item(Item=item)

    logger.info("Created bug report ticket: %s", ticket_id)

    return item


# Alias matching the CloudFormation Lambda Handler setting (index.handler
# style naming isn't required here since the template points at
# create_bug_report.handler directly).
lambda_handler = handler
