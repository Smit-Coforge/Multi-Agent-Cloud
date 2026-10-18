# Risk agent on ECS Fargate

One Fargate task. It scores `deploy/sample-input.json` (C-001, Alex Rivera) with `amazon.nova-lite-v1:0` in `us-east-1` and prints the risk object. CloudWatch is that stdout.

The laptop stays on Ollama, `qwen3:8b`, at `http://host.docker.internal:11434`. This task calls Bedrock.

## Architecture

One model call sends the name, age, zip, and driving record to Nova in `us-east-1`. On the laptop that call stays on the machine. An air-gapped site cannot use this image as written: `deploy/Dockerfile` pulls `python:3.12-slim` and installs from the public index, and the weights have to already be inside the boundary.

Steps 1–6 are the deployment, the network, and the access control. The comparison at the end is the cost, the complexity, and the recommendation.

Console steps are the path. The blocks below are the same actions as commands, with placeholders, for the parts the console cannot do or that are easier to read as a shape.

## 1. Image

`deploy/Dockerfile` is the image. It installs the app dependencies, copies `src` and `deploy/sample-input.json`, and leaves the case at `deploy/sample-input.json` inside the container. Build it on the machine that has the repo. ECR stores an image; it does not build one.

```text
docker build -f deploy/Dockerfile -t risk-agent .
```

The task definition command scores that file and prints the risk object. That print is the CloudWatch log.

## 2. Roles

IAM → Roles → Create role. Trusted entity: Elastic Container Service Task. Only an ECS task can assume either role.

Two roles, so a bug in the scorer cannot pull images or write logs. ECS itself uses the execution role while it starts the container. The process inside the container uses the task role.

**risk-agent-execution.** Pull this image and write this log group.

- `ecr:GetAuthorizationToken` on `*` lets ECS sign in to ECR. ECR will not mint that token for a single repository. The token is a login, not permission to pull every image in the account.
- `ecr:BatchCheckLayerAvailability`, `ecr:GetDownloadUrlForLayer`, and `ecr:BatchGetImage` on `repository/risk-agent` let ECS download this image.
- `logs:CreateLogStream` and `logs:PutLogEvents` on `/ecs/risk-agent` let ECS open a stream and write lines. The log group is created in step 4, so `logs:CreateLogGroup` stays off the role.

**risk-agent-task.** The model call.

- `bedrock:InvokeModel` on `arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0`. One model, this region. A second model, or the same model in another region, is denied.

```text
aws iam create-role --role-name risk-agent-execution \
  --assume-role-policy-document <trust ecs-tasks.amazonaws.com>

aws iam put-role-policy --role-name risk-agent-execution \
  --policy-name risk-agent-execution \
  --policy-document <ECR login, pull repository/risk-agent, write /ecs/risk-agent>

aws iam create-role --role-name risk-agent-task \
  --assume-role-policy-document <trust ecs-tasks.amazonaws.com>

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

The task starts, scores one file, and exits. Nothing calls it. It calls ECR, S3, CloudWatch Logs, and Bedrock.

Run it in a private subnet, `10.40.2.0/24` inside `10.40.0.0/16`, in `us-east-1` with the image and the model. Public IP off. The route table starts as `10.40.0.0/16` local. `assignPublicIp` disabled and that route table cannot pull or log: ECR, CloudWatch Logs, and S3 are not addresses in that CIDR, and there is no default route out.

Create the private path in the console before Run task. VPC → Endpoints → Create endpoint.

- Interface endpoints, private DNS on, in `10.40.2.0/24`: `com.amazonaws.us-east-1.ecr.api` (the login token), `com.amazonaws.us-east-1.ecr.dkr` (layer metadata), `com.amazonaws.us-east-1.logs` (the log lines), and `com.amazonaws.us-east-1.bedrock-runtime` (InvokeModel). Private DNS makes the normal regional names resolve to those endpoints. Their security group allows 443 from the task.
- Gateway endpoint `com.amazonaws.us-east-1.s3` on the subnet's route table. ECR layer URLs point at S3. The gateway adds the prefix-list route `pl-63a5400a` (S3 in us-east-1) beside `10.40.0.0/16`.

The task security group has no inbound rule, because nothing connects to the task. Outbound is 443 to the endpoint security group and to `pl-63a5400a`.

```text
aws ec2 create-vpc-endpoint --vpc-endpoint-type Interface --private-dns-enabled \
  --service-name com.amazonaws.us-east-1.ecr.api \
  --subnet-ids <10.40.2.0/24> --security-group-ids <endpoint group>
# same shape for ecr.dkr, logs, and bedrock-runtime

aws ec2 create-vpc-endpoint --vpc-endpoint-type Gateway \
  --service-name com.amazonaws.us-east-1.s3 \
  --route-table-ids <table that has only 10.40.0.0/16>
# route table then has: 10.40.0.0/16 local, pl-63a5400a to this gateway
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

CloudWatch → Log groups → `/ecs/risk-agent` → the stream for this task. The lines are the start line and the risk object for C-001:

```text
risk-agent start case_id=C-001
{"score": 0.22, "band": "low", "factors": ["..."], "underwriter_note": "..."}
```

`src/agents/risk.py` keeps that object only when the score is between 0 and 1, the band matches the score, the factors and note are non-empty, and a file with no DUI has no dui factor.

## Comparison

| | Laptop | This task |
|---|---|---|
| What you operate | Ollama, `qwen3:8b`, on the machine | The Fargate task (0.5 vCPU, 1 GB), two IAM roles, the private subnet, four interface endpoints (`ecr.api`, `ecr.dkr`, CloudWatch Logs, Bedrock Runtime), and the S3 gateway. AWS operates Nova. |
| Cost | $0. The weights are local. | Fargate for the run, the four interface endpoints while they exist, and Nova tokens for the call. |
| C-001 reply | 0.2 / low, from `report/C-001/20261018T212338700114Z.json` | 0.22 / low, the simulated Nova reply in step 6 |

Both replies are a low band on the same clean file. Use the laptop when the file cannot leave the premises. Use this task when it can.
