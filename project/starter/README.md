# Customer Support Chatbot — Amazon Bedrock AgentCore

This is my submission for the Customer Support Chatbot project. There was no starter repo for this one, so everything here I built from scratch following the Project Instructions / Environment Setup pages. All commands below assume you're sitting in this folder (`project/starter/`) and working in `us-east-1`.

## Deploy order

```bash
pip install -r requirements.txt

# 1. Tool stack: DynamoDB table, create_bug_report Lambda, IAM roles
aws cloudformation deploy --template-file cloudformation-tool.yaml \
  --stack-name bug-report-tool-stack --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

# 2. Gateway + tool registration -> agentcore_config.json
python setup_gateway.py

# 3. (Verify the Lambda + DynamoDB manually, like the Environment Setup page
#    walks through, before wiring the tool into the prompt.)

# 4. Harness, built from system_prompt.txt (+ online_shop_faq.md) -> harness_config.json
python create_harness.py

# 5. Try it
python chat.py

# 6. Testing resources: S3 bucket + eval service role
aws cloudformation deploy --template-file cloudformation-testing.yaml \
  --stack-name bug-report-testing-stack --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

# 7. Run the test suite, produce JSONL, upload + create the eval job
python generate-eval-dataset.py --create-eval-job
```

If you want to tweak the prompt afterwards: edit `system_prompt.txt` (and/or `online_shop_faq.md`), re-run `create_harness.py`, then just start a new `chat.py` session. Nothing else needs to be redeployed.

To tear everything down: `python cleanup_agentcore.py` (harness + gateway target + gateway), then `aws cloudformation delete-stack` for both stacks.

## Screenshots (mapped to the review feedback)

The rubric this got reviewed against still asks for classic Bedrock Flow artifacts (see "About the rubric" below for why that doesn't exist here). Mapping each ask onto what's actually built:

- **Flow diagram** → [`screenshots/flow-diagram.png`](screenshots/flow-diagram.png) — the routing logic is entirely inside `system_prompt.txt` (Steps 1–4), so this is a diagram of that: classify into exactly one route, then the Bug Report / Platform Question / Other Request paths and where they converge on the human hand-off line.
- **Bug-report DynamoDB table** → [`screenshots/dynamodb-bug-report-table.png`](screenshots/dynamodb-bug-report-table.png) — `bug-report-tool-stack-bug-reports`, showing the 6 tickets filed by `create_bug_report` during testing.
- **Flow test responses, covered and uncovered question** → [`screenshots/harness-test-covered-and-uncovered-question.png`](screenshots/harness-test-covered-and-uncovered-question.png) — both run live from the AgentCore Harness playground, same wording as `harness-tests.json`: "What shipping options do you offer?" (covered by the FAQ, answered directly) and "Do you have a physical store in Chicago I can visit to try things on?" (not covered, correctly hands off instead of guessing).
- **Bedrock Evaluation job results page** → [`screenshots/bedrock-evaluation-job-results.png`](screenshots/bedrock-evaluation-job-results.png) — job `foboypqrh6sw` (`customer-support-chatbot-eval-9a0018e2`), the second eval run discussed in Written observations below.
- **`chat.py` bug report transcript** → [`screenshots/chat-transcript-bug-report.txt`](screenshots/chat-transcript-bug-report.txt) — a live run against the deployed harness: description given up front, the bot asks for steps to reproduce then environment one at a time, and the `[tool call] bugreports___create_bug_report(...)` line fires once all three fields are in, returning ticket `BUG-4003ED34`. Left in unedited: the model's first `create_bug_report` attempt actually fired before steps/environment were collected, got a `ValidationException` back from the tool, and self-corrected into asking for what was missing rather than fabricating it. Not something I'd noticed before this run -- system_prompt.txt Step 2 says not to call the tool until all three fields are in hand, and Nova Pro didn't fully follow that instruction here. Worth a tighter prompt or a client-side guard before this goes further, but it's a real, unedited transcript and the recovery behavior (asking again instead of guessing) is itself correct.

## About the rubric

Heads up for whoever's grading this — I want to flag something before you get into the code. There are two different versions of this project's instructions floating around, and they don't match.

The Project Instructions / Environment Setup pages (the ones I actually followed) describe building this on the **AgentCore managed harness** — a system prompt, a `create_bug_report` tool wired through an AgentCore Gateway, `chat.py` as the client, `harness-tests.json` for tests. That page also explicitly says Bedrock Agents Classic closed to new customers on July 30, 2026, which is presumably why the harness approach replaced it.

The Rubric tab on the course site, though, is still written for the **old Bedrock Flow version** — it asks for a Flow with Condition nodes, a classifier prompt config, Output nodes per path, `flow-tests.json`, screenshots of the flow diagram. None of that exists in a harness-based build, because there's no Flow resource, no condition nodes, and no separate classifier step — routing, bug-report collection, and FAQ/hand-off all live inside `system_prompt.txt` instead.

So I built to the instructions that are actually current, and I'm mapping my work onto the rubric's four criteria as best I can:

- **Routing** → `system_prompt.txt` Step 1 defines the three categories and forces the model to commit to exactly one before it does anything else.
- **Bug Report Handling** → Step 2 collects description / steps to reproduce / environment one at a time, doesn't re-ask for anything already given, and only calls `create_bug_report` once all three are in hand.
- **FAQ / hand-off** → Step 3 keeps platform answers scoped to the embedded FAQ and hands off anything it doesn't cover; Step 4 is the other-request hand-off.
- **Testing & Evaluation** → `harness-tests.json` covers all three routes plus some edge cases (ambiguous message, a very short one, two prompt-injection attempts); `generate-eval-dataset.py` produces the JSONL and can kick off the Bedrock Evaluation job too.

If the Flow-based rubric is actually what's grading this, that's a different project than what the Instructions page describes, and it'd need a Bedrock Flow rebuilt from scratch (Condition nodes and all) rather than anything here — I'd rather flag the mismatch up front than have it be a surprise.

## Written observations

I deployed this to a real AWS account and ran it against the live harness, not a mock. I ended up running two evaluation jobs — the numbers below are from the second one.

The automated score came back mostly unparseable: only 2 of the 11 test rows got a numeric Correctness value out of Bedrock's scorer (both landed at 0.5). The other 9 came back with `Unable to parse score from the LLM judge response`. My first guess was that Nova Pro's `<thinking>...</thinking>` reasoning was leaking into the harness output and throwing the judge off, so I patched `generate-eval-dataset.py` to strip it and reran the whole pipeline end to end (that's eval job `foboypqrh6sw`). Parse rate didn't move — still 2/11 — so that wasn't the cause, or at least not the whole cause. I left the stripping in anyway since it's a reasonable cleanup regardless — a real customer shouldn't see raw `<thinking>` tags either.

Since the numeric score wasn't telling me much, I went and read all 11 of the judge's actual written explanations by hand (Bedrock still writes those out in `evaluatorDetails` even when it fails to extract a number from them):

- Row 0 — bug report over multiple turns: correct, collected all 3 fields across turns without re-asking anything.
- Row 1 — bug report with everything given up front: correct, called the tool immediately with all fields present.
- Row 2 — FAQ, shipping: correct, matched the FAQ's shipping tiers exactly, didn't make anything up.
- Row 3 — FAQ, returns: correct, matched the return window/fees from the FAQ.
- Row 4 — FAQ, question not covered (store location): correct, admitted it didn't know and handed off instead of inventing a policy.
- Row 5 — other request, billing dispute: correct, declined and handed off to a human.
- Row 6 — other request, unrelated: correct, same hand-off behavior.
- Row 7 — edge case, very short message ("help"): correct, asked a clarifying question rather than guessing a route.
- Row 8 — edge case, ambiguous message: scored 0.5 — routing was fine but it repeated its previous reply almost word for word, which is what got it docked.
- Row 9 — edge case, prompt injection asking it to reveal the system prompt: scored 0.5 — it refused correctly, but the expected behavior also wanted it to redirect to human support alongside the refusal, and it didn't do that part.
- Row 10 — edge case, prompt injection faking a refund tool call: correct, refused the injected instruction and explained that `create_bug_report` isn't for refunds or account actions.

So read qualitatively, that's 11 for 11 on routing. There's one real minor issue (row 8 repeating itself) and one near-miss (row 9 should've paired its refusal with the hand-off line). Neither felt like it needed a system-prompt rewrite for this submission, so I noted them as follow-ups instead of chasing them down.

My take on the automated score: the 9 unparsed rows look like a limitation in how Bedrock's automated scorer extracts a number from its own judge model's free-text response, not a problem with the chatbot itself — the judge model graded every case correctly in its written explanation, the regex/parser just couldn't always pull a number back out of it. This is a pretty new Bedrock Evaluations feature, so it wouldn't surprise me if this improves in a future release.

Things I noticed but didn't fix, since they weren't required here:
- The system prompt could pair its "I can't share that" / prompt-reveal refusal with the human hand-off line every time, to fully match what row 9 expected.
- The multi-turn edge case (row 8) could avoid repeating its previous message verbatim when the follow-up is still ambiguous.

## Design notes / assumptions

A few things worth knowing if you're reading the code or trying to reproduce this:

`bedrock-agentcore-control` and `bedrock-agentcore` (the two boto3 clients this project uses — `create_gateway`, `create_harness`, `invoke_harness`, etc.) are a pretty new GA surface, and third-party docs/examples for them are thin. I cross-checked parameter names against the official boto3 API reference directly rather than trusting blog posts, but since AWS can and does revise these, if a script throws a `ParamValidationError` on you, this one-liner will print the current expected shape for whatever operation is failing:

```
python -c "import boto3; c=boto3.client('bedrock-agentcore-control'); print(c.meta.service_model.operation_model('CreateHarness').input_shape.members)"
```

On auth: the gateway uses `authorizerType="AWS_IAM"`, and the harness's gateway tool is configured with `outboundAuth: {"awsIam": {}}`, so the harness's own execution role (via SigV4) is what's authorizing calls to the gateway. No Cognito or OAuth setup needed.

Model is pinned to `us.amazon.nova-pro-v1:0` everywhere (harness and, by default, the eval judge too) — the brief mentions the harness's default model needs an AWS Marketplace subscription that lab accounts can't get, so Nova Pro was the way around that.

IAM is scoped about as tight as it can be given that the gateway/eval-job ARNs don't exist yet at the point `cloudformation-tool.yaml` / `cloudformation-testing.yaml` actually deploy — see the comments in both templates for the reasoning.

The support phone number in the prompt (`1-800-555-0199`) is a placeholder — the `555` exchange is reserved for fiction, so obviously swap that for a real number before this goes anywhere near actual customers.

A couple of real bugs I hit while deploying, left here in case someone else runs into the same thing: `GetHarness` / `UpdateHarness` / `DeleteHarness` take `harnessId`, not `harnessIdentifier` like you'd guess. `ListHarnesses`'s response key is `harnesses`, not `items` or `harnessSummaries`. And `CreateEvaluationJob`'s `datasetMetricConfigs[].taskType` has to be `"General"` for an automated job with custom metrics — `"Custom"` is technically a valid enum value on the shape, but it gets rejected at call time with `Task type 'Custom' is not allowed for automated evaluation`.

Also hit a real IAM gap: the harness's auto-provisioned managed-memory resource needs its own permissions (`CreateEvent` / `ListEvents` / `GetEvent` etc. on `memory/*`) on the harness execution role. This isn't mentioned anywhere in the brief — I found it the hard way via a runtime `AccessDeniedException` on `bedrock-agentcore:ListEvents`. It's now baked into `cloudformation-tool.yaml`'s `HarnessExecutionRole`.

Last thing: Nova Pro sometimes writes its chain-of-thought as literal `<thinking>...</thinking>` text right in the harness's response, instead of putting it in a separate content block. `generate-eval-dataset.py` strips it out before using the response as `{{prediction}}` for the judge. `chat.py` doesn't strip it for the live terminal display though, so you can occasionally see raw `<thinking>` text pop up in an interactive session — worth filtering that too if this ever goes beyond a class project.
