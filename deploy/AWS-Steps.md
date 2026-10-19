# Risk agent on ECS Fargate

One Fargate task. It scores `deploy/sample-input.json` (C-001, Alex Rivera) with `amazon.nova-lite-v1:0` in `us-east-1` and prints the risk object. CloudWatch is that stdout.

The laptop stays on Ollama, `qwen3:8b`, at `http://host.docker.internal:11434`. This task calls Bedrock.

## Architecture

One model call sends the name, age, zip, and driving record to Nova in `us-east-1`. On the laptop that call stays on the machine. A client requires that when a residency or vendor rule forbids sending that file to Nova. An air-gapped site cannot use this image as written: `deploy/Dockerfile` pulls `python:3.12-slim` and installs from the public index, and the weights have to already be inside the boundary.

Steps 1–6 are the deployment, the network, and the access control. The comparison at the end is the cost, the complexity, and the recommendation.

Console steps are the path. The blocks below are the same actions as commands, with placeholders, for the parts the console cannot do or that are easier to read as a shape.

## 1. Image

`deploy/Dockerfile` is the image. It installs the app dependencies, copies `src` and `deploy/sample-input.json`, and leaves the case at `deploy/sample-input.json` inside the container. Build it on the machine that has the repo. ECR stores an image; it does not build one.

```text
docker build -f deploy/Dockerfile -t risk-agent .
```

The task definition command scores that file and prints the risk object. That print is the CloudWatch log. The command, in the shape the container would run:

```text
read deploy/sample-input.json
call InvokeModel for amazon.nova-lite-v1:0 in us-east-1, with the task role
keep the object only when the checks in src/agents/risk.py pass
print the start line and the risk object
```

## 2. Roles

IAM → Roles → Create role. Trusted entity: Elastic Container Service Task. Each trust policy allows `ecs-tasks.amazonaws.com` to assume the role only when `aws:SourceArn` is the `risk-agent` task definition, `arn:aws:ecs:us-east-1:<account>:task-definition/risk-agent:*`. Any other ECS task in the account is denied.

Two roles, so a bug in the scorer cannot pull images or write logs. ECS itself uses the execution role while it starts the container. The process inside the container uses the task role.

**risk-agent-execution.** Pull this image and write this log group.

- `ecr:GetAuthorizationToken` on `*` lets ECS sign in to ECR. ECR will not mint that token for a single repository. The token is a login, not permission to pull every image in the account.
- `ecr:BatchCheckLayerAvailability`, `ecr:GetDownloadUrlForLayer`, and `ecr:BatchGetImage` on `repository/risk-agent` let ECS download this image.
- `logs:CreateLogStream` and `logs:PutLogEvents` on `/ecs/risk-agent` let ECS open a stream and write lines. The log group is created in step 4, so `logs:CreateLogGroup` stays off the role.

**risk-agent-task.** The model call.

- `bedrock:InvokeModel` on `arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0`. One model, this region. A second model, or the same model in another region, is denied.

```text
aws iam create-role --role-name risk-agent-execution \
  --assume-role-policy-document <ecs-tasks.amazonaws.com, aws:SourceArn = task-definition/risk-agent:*>

aws iam put-role-policy --role-name risk-agent-execution \
  --policy-name risk-agent-execution \
  --policy-document <ECR login, pull repository/risk-agent, write /ecs/risk-agent>

aws iam create-role --role-name risk-agent-task \
  --assume-role-policy-document <ecs-tasks.amazonaws.com, aws:SourceArn = task-definition/risk-agent:*>

aws iam put-role-policy --role-name risk-agent-task \
  --policy-name risk-agent-task \
  --policy-document <bedrock:InvokeModel on amazon.nova-lite-v1:0 in us-east-1>
```

## 3. Repository

ECR → Create repository → `risk-agent`, region `us-east-1`. The repository URI looks like `<account>.dkr.ecr.us-east-1.amazonaws.com/risk-agent`.

The console cannot upload a local image. From the machine that built it, sign in, tag, and push.

```text
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com

docker tag risk-agent:latest <account>.dkr.ecr.us-east-1.amazonaws.com/risk-agent:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/risk-agent:latest
```

## 4. Task definition

CloudWatch → Log groups → Create `/ecs/risk-agent` in `us-east-1`. The execution role can write a stream in that group. It cannot create the group.

ECS → Task definitions → Create. This is the shape ECS runs.

- Launch type Fargate, 0.5 vCPU, 1 GB. One short score, then the task exits.
- Image: the URI from step 3, tag `latest`
- Execution role `risk-agent-execution`, task role `risk-agent-task`
- Logs: awslogs, group `/ecs/risk-agent`, region `us-east-1`, stream prefix `ecs`
- Command scores `deploy/sample-input.json` and prints the risk object. No Ollama host. The model is the foundation-model ARN on the task role.

```text
aws logs create-log-group --log-group-name /ecs/risk-agent --region us-east-1

aws ecs register-task-definition --family risk-agent --network-mode awsvpc \
  --requires-compatibilities FARGATE --cpu 512 --memory 1024 \
  --execution-role-arn arn:aws:iam::<account>:role/risk-agent-execution \
  --task-role-arn arn:aws:iam::<account>:role/risk-agent-task \
  --container-definitions <image, awslogs to /ecs/risk-agent, command scores the sample>
```

## 5. Network

Assumption. This VPC was not created. The ids are the shape of one network in `us-east-1`, not resources from an account. VPC `10.40.0.0/16`. Private subnet `10.40.2.0/24`. One task security group and one endpoint security group. The S3 prefix list `pl-63a5400a` is AWS's published list for S3 in `us-east-1`, not an invented id. The endpoints below are assumed in that VPC. DNS resolution and DNS hostnames on the VPC are assumed on, which is what lets private DNS resolve.

The task starts, scores one file, and exits. Nothing connects to it. It needs a private path to four services, all in `us-east-1`: ECR for the image, S3 for the image layers, CloudWatch Logs for the printed score, and Bedrock for Nova.

The task runs in the private subnet with no public address. The subnet's only starting route is the VPC itself, so those services are out of reach until the endpoints exist. The endpoints are the private doors. Interface endpoints, with private DNS, for ECR, CloudWatch Logs, and Bedrock Runtime. ECR is two doors, one for the login and one for the layer locations. One S3 gateway on the subnet route table, which adds `pl-63a5400a` beside the local route. ECR returns an S3 address for the layers, so that gateway is the hop that downloads them.

The task security group has no inbound rule. Outbound is HTTPS to the endpoint group and to the S3 prefix list. The endpoint group accepts HTTPS only from the task group.

```text
# assumed: interface endpoints in 10.40.2.0/24, private DNS on
#   ecr.api, ecr.dkr, logs, bedrock-runtime
# assumed: S3 gateway on the subnet route table
#   route table then has 10.40.0.0/16 local, and pl-63a5400a to the gateway
```

## 6. Run

ECS → Clusters → `underwriting-cluster` → Run new task. Create the cluster first if it is not there: ECS → Clusters → Create, name `underwriting-cluster`.

- Fargate, task definition `risk-agent`
- Subnet `10.40.2.0/24`, the task security group, public IP off

The task moves to RUNNING once the image is pulled through the ECR endpoints and the S3 prefix-list route. It stops with exit code 0 when the container prints the object and exits. ECS used `risk-agent-execution` for the pull and the logs. The container used `risk-agent-task` for `InvokeModel`.

```text
aws ecs run-task --cluster underwriting-cluster --task-definition risk-agent \
  --launch-type FARGATE \
  --network-configuration awsvpcConfiguration={
      subnets=[<10.40.2.0/24>],
      securityGroups=[<task group>],
      assignPublicIp=DISABLED
    }
```

CloudWatch → Log groups → `/ecs/risk-agent` → the stream for this task. Nova was not called. The object below is a written stand-in for what that log would contain. Each factor is a fact already on `deploy/sample-input.json`. The score is 0.22 so the band is low, the note is non-empty, and the file has no DUI. Assumption: a second model on this clean file stays in the low band, close to the local 0.2. The 0.02 gap is that assumption. It is not a measured claim that Nova is a better model.

```text
risk-agent start case_id=C-001
{"score": 0.22, "band": "low", "factors": ["Age 34", "16 years licensed", "8000 miles a year", "2019 Honda Civic", "Valid license", "No violations on the MVR", "Low-risk territory 94110"], "underwriter_note": "Clean record, valid license, and low mileage support a low score."}
```

`src/agents/risk.py` keeps that object only when the score is between 0 and 1, the band matches the score, the factors and note are non-empty, and a file with no DUI has no dui factor.

## Comparison

| | Laptop | This task |
|---|---|---|
| What you operate | Ollama, `qwen3:8b`, on the machine | The Fargate task (0.5 vCPU, 1 GB), two IAM roles, the private subnet, four interface endpoints (`ecr.api`, `ecr.dkr`, CloudWatch Logs, Bedrock Runtime), and the S3 gateway. AWS operates Nova. |
| Cost | $0. Measured: a local model has no API charge. | Assumption, from public list prices, not a bill from this account. One short Fargate run is well under a cent. The four interface endpoints are about $0.01 per endpoint per hour for as long as they exist, which is the standing cost. Nova is priced per token on this one call. |

`report/C-001/20261018T212338700114Z.json` is the full pipeline on `data/applications/C-001.json`. The two replies below are the risk agent on the same file, `deploy/sample-input.json`.

Local Ollama, run on that file:

```json
{"score": 0.2, "band": "low", "factors": ["Valid license", "No at-fault accidents", "No violations", "No undisclosed items", "Low-risk territory"], "underwriter_note": "Driver has a clean record with no risk factors identified."}
```

Simulated Nova, the step 6 stand-in. Same assumption as that step: low band, near the local score, not a live Nova reply and not a quality ranking.

```json
{"score": 0.22, "band": "low", "factors": ["Age 34", "16 years licensed", "8000 miles a year", "2019 Honda Civic", "Valid license", "No violations on the MVR", "Low-risk territory 94110"], "underwriter_note": "Clean record, valid license, and low mileage support a low score."}
```

Both are a low band. Use the laptop when the file cannot leave the premises. Use this task when it can.
