# Risk agent on ECS Fargate

Plan for one Fargate task. It reads one applicant and one enrichment record, calls the model, and prints a risk object. CloudWatch is that stdout.

Ids such as `123456789012`, `subnet-08f1a2b3c4d5e6f70`, and `sg-0b91c2d3e4f567890` are stand-ins for what these commands would return.

## Options

**Where it runs.** Fargate. The repo already has an image, and a full case in the traces is about 10–12 seconds, which is batch work. A function could hold this one short call. Kubernetes would add a control plane this lab does not need.

**Model.** Ollama, `qwen3:8b`, through the same `complete()` call as the laptop. The laptop host is `http://host.docker.internal:11434`. This task uses `http://ollama.underwriting.internal:11434`. The checks in `src/agents/risk.py` stay put. Ollama is the dev stand-in. A production host for the same weights would be vLLM.

**If the file may go to a cloud model.** Same task, Bedrock in-region. The task role gains the one statement in the role section. The printed object stays the same.

**If a regulator says the file cannot leave the country.** On-prem. The file is a name, age, zip, and driving record, and one model call sends that file. Reasons: residency, sovereignty, vendor approval, and a model version the carrier controls. An air-gapped site would load the base image, the wheels, and the weights first. `deploy/Dockerfile` pulls public `python:3.12-slim` and runs `pip install`.

## 1. Build

`deploy/Dockerfile`, from the repo root. The case in the image is `deploy/sample-input.json` (C-001, Alex Rivera, clean file).

```text
$ docker build -f deploy/Dockerfile -t risk-agent .
Successfully built 4c1e9a2b7d10
Successfully tagged risk-agent:latest
```

## 2. Roles

Fargate uses two roles. The execution role is for ECS itself: pull the image and send logs. The task role is for the code inside the container. A bug in the scorer should not be able to use the pull-and-log permissions, so they are split.

Both trust only `ecs-tasks.amazonaws.com`, which means only an ECS task can assume them. Files: `deploy/iam/execution-role-trust.json`, `deploy/iam/task-role-trust.json`.

```bash
aws iam create-role \
  --role-name risk-agent-execution \
  --assume-role-policy-document file://deploy/iam/execution-role-trust.json

aws iam put-role-policy \
  --role-name risk-agent-execution \
  --policy-name risk-agent-execution \
  --policy-document file://deploy/iam/execution-role-policy.json

aws iam create-role \
  --role-name risk-agent-task \
  --assume-role-policy-document file://deploy/iam/task-role-trust.json
```

Execution policy (`deploy/iam/execution-role-policy.json`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "PullRiskAgentImage",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage"
      ],
      "Resource": "arn:aws:ecr:us-east-1:123456789012:repository/risk-agent"
    },
    {
      "Sid": "WriteRiskAgentLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:us-east-1:123456789012:log-group:/ecs/risk-agent:*"
    }
  ]
}
```

What that policy allows:

- **Login token.** `ecr:GetAuthorizationToken` has to be on `*`. ECR will not mint the token for a single repository. The token lets ECS sign in. It does not let ECS pull every image in the account.
- **Pull this image.** `BatchCheckLayerAvailability`, `GetDownloadUrlForLayer`, and `BatchGetImage` are limited to `repository/risk-agent`. ECS can download that one image. It cannot download any other repository in the account.
- **Write this log group.** `logs:CreateLogStream` and `logs:PutLogEvents` are limited to `/ecs/risk-agent`. The task can open a stream there and write lines. It cannot create log groups, and it cannot write to another application's logs. The group itself is created in step 4, so `logs:CreateLogGroup` is left off.

Nothing else is on the role. No S3, no Secrets Manager, no IAM changes. If this task were compromised, the AWS damage is pulling one image and writing one log group.

`risk-agent-task` has no permissions policy. The container reads the case file baked into the image and calls Ollama at a private hostname. That call is ordinary network traffic, not an AWS API, so the code has no AWS permissions to misuse.

If the task called Bedrock instead, this one statement would be attached to the task role. It allows invoking a single model in `us-east-1`. A second model, or the same model in another region, would be denied. The execution role would stay as written above.

```json
{
  "Sid": "InvokeOneModel",
  "Effect": "Allow",
  "Action": "bedrock:InvokeModel",
  "Resource": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"
}
```

## 3. Push

```text
$ aws ecr create-repository --repository-name risk-agent --region us-east-1
repositoryUri: 123456789012.dkr.ecr.us-east-1.amazonaws.com/risk-agent

$ aws ecr get-login-password --region us-east-1 \
    | docker login --username AWS --password-stdin \
      123456789012.dkr.ecr.us-east-1.amazonaws.com
Login Succeeded

$ docker tag risk-agent:latest \
    123456789012.dkr.ecr.us-east-1.amazonaws.com/risk-agent:latest

$ docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/risk-agent:latest
latest: digest: sha256:a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456 size: 2412
```

## 4. Register

`deploy/task-definition.json`: that image, both role ARNs, Fargate, 0.5 vCPU, 1 GB, logs to `/ecs/risk-agent`.

```text
$ aws logs create-log-group --log-group-name /ecs/risk-agent --region us-east-1

$ aws ecs register-task-definition \
    --cli-input-json file://deploy/task-definition.json \
    --region us-east-1
taskDefinitionArn: arn:aws:ecs:us-east-1:123456789012:task-definition/risk-agent:1
revision: 1
status: ACTIVE
executionRoleArn: arn:aws:iam::123456789012:role/risk-agent-execution
taskRoleArn: arn:aws:iam::123456789012:role/risk-agent-task
```

## 5. Network

The network is chosen by listing who talks to whom. This task starts, scores one file, and exits. Nobody calls it. It calls three things.

**Which network.** Fargate gives the task its own address in a subnet we name on `run-task`. The subnet is `subnet-08f1a2b3c4d5e6f70`, `10.40.2.0/24`, in `us-east-1a`, inside `vpc-0c4e8a91b2d34f701` (`10.40.0.0/16`). Same region as the ECR image, so the pull stays local.

It is a private subnet: the route table has `10.40.0.0/16` and no path to an internet gateway. `assignPublicIp` is `DISABLED`. The sample run gets `10.40.2.47`. A public subnet is for something people on the internet connect to. This job is not that, so it does not get a public address.

Pulling the image and writing logs still have to work. Those go to VPC endpoints in the same VPC, which are private doors to ECR, S3, and CloudWatch. The task never needs the public internet.

**How a rule is picked.** Port is the port that service already listens on. "To" is that service's security group, or AWS's address list when the service is S3. Nothing else is opened.

| Who the task calls | Port | To | Where that came from |
|---|---|---|---|
| ECR and CloudWatch Logs | 443 | `sg-0aa11bb22cc33dd44` | Both speak HTTPS. Their VPC endpoints sit in this group. |
| S3, for the image layers | 443 | `pl-63a5400a` | ECR stores layers in S3. `pl-63a5400a` is AWS's list of S3 addresses in us-east-1. |
| Ollama | 11434 | `sg-0777888999aabbccd` | `OLLAMA_HOST` is `http://ollama.underwriting.internal:11434`. 11434 is the port in that URL. The group is the Ollama host's group. |

The task group is `sg-0b91c2d3e4f567890`. Inbound is empty, because nothing connects to the task. Outbound is only the three rows above.

The other direction is locked the same way. The endpoint group accepts 443 only from the task group. Ollama's group accepts 11434 only from the task group.

A Bedrock task would change one row. Port 443, to the Bedrock endpoint's group, because `bedrock:InvokeModel` is HTTPS. The 11434 row would go away. The private subnet would stay.

## 6. Run

```text
$ aws ecs create-cluster --cluster-name underwriting-cluster --region us-east-1
clusterArn: arn:aws:ecs:us-east-1:123456789012:cluster/underwriting-cluster
status: ACTIVE

$ aws ecs run-task \
    --cluster underwriting-cluster \
    --task-definition risk-agent \
    --launch-type FARGATE \
    --network-configuration 'awsvpcConfiguration={subnets=[subnet-08f1a2b3c4d5e6f70],securityGroups=[sg-0b91c2d3e4f567890],assignPublicIp=DISABLED}' \
    --region us-east-1
taskArn: arn:aws:ecs:us-east-1:123456789012:task/underwriting-cluster/a1b2c3d4e5f6478990abcdef12345678
lastStatus: PROVISIONING
launchType: FARGATE
taskDefinitionArn: arn:aws:ecs:us-east-1:123456789012:task-definition/risk-agent:1
privateIPv4Address: 10.40.2.47

$ aws ecs describe-tasks \
    --cluster underwriting-cluster \
    --tasks a1b2c3d4e5f6478990abcdef12345678 \
    --region us-east-1
lastStatus: STOPPED
stopCode: EssentialContainerExited
exitCode: 0
```

The task definition on that run is `risk-agent:1`, so ECS pulled the image and wrote the logs with `risk-agent-execution`.

## 7. Sample run

The task in step 6 scores `deploy/sample-input.json` and prints the result. CloudWatch is that print. This is the check that the run produced the right object for this file.

```text
$ aws logs get-log-events \
    --log-group-name /ecs/risk-agent \
    --log-stream-name ecs/risk-agent/a1b2c3d4e5f6478990abcdef12345678 \
    --region us-east-1
risk-agent start case_id=C-001
{"score": 0.22, "band": "low", "factors": ["Age 34", "16 years licensed", "8000 miles a year", "2019 Honda Civic", "Valid license", "No violations on the MVR", "Low-risk territory 94110"], "underwriter_note": "Clean record, valid license, and low mileage support a low score."}
```

The container does this:

```text
input = sample-input.json          # case C-001
risk  = risk_scoring_agent(input.applicant, input.enrichment)

keep risk only when:
    0 <= risk.score <= 1
    risk.band == "low"    if score < 0.4
    risk.band == "medium" if score <= 0.7
    risk.band == "high"   otherwise
    risk.factors is a non-empty list of facts on this file
    risk.underwriter_note is non-empty
    "dui" appears in factors only if the file has a DUI
```

Input the task scored:

```text
applicant:  Alex Rivera, age 34, licensed 16 years
            2019 Honda Civic, 8000 miles, zip 94110
            violations [], accidents 0
enrichment: license valid, MVR violations [], at-fault accidents 0
            territory 94110 risk_band low
            nondisclosure [], surcharge false
```

Output the log shows, and why it passes:

```text
score 0.22              in range
band  "low"             0.22 < 0.4
factors                 each line is a field above
note  "Clean record..." non-empty
dui                     absent, and the file has no DUI
```

A Bedrock call would come back through the same check and print the same four keys.
