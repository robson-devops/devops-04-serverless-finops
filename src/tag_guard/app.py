"""Avisa quando uma instância EC2 entra em execução sem as tags obrigatórias."""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

COMPLIANCE_TAG = "TagCompliance"


def missing_tags(tags, required):
    present = {t["Key"] for t in tags if t.get("Value", "").strip()}
    return [key for key in required if key not in present]


def handler(event, context):
    instance_id = event["detail"]["instance-id"]
    region = event.get("region", os.environ.get("AWS_REGION", "us-east-1"))
    required = [k.strip() for k in os.environ["REQUIRED_TAGS"].split(",") if k.strip()]
    ec2 = boto3.client("ec2")

    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    instance = reservations[0]["Instances"][0]
    missing = missing_tags(instance.get("Tags", []), required)

    if not missing:
        logger.info(json.dumps({"instance": instance_id, "compliant": True}))
        return {"instance": instance_id, "missing": []}

    # Só marca e avisa; parar a instância fica a cargo de quem é dono dela.
    ec2.create_tags(
        Resources=[instance_id],
        Tags=[{"Key": COMPLIANCE_TAG, "Value": "faltando:" + "+".join(missing)}],
    )
    boto3.client("sns").publish(
        TopicArn=os.environ["TOPIC_ARN"],
        Subject=f"[FinOps] Instância sem tags obrigatórias: {instance_id}"[:100],
        Message="\n".join(
            [
                f"A instância {instance_id} ({instance['InstanceType']}) entrou em execução na região {region}",
                f"sem as tags obrigatórias: {', '.join(missing)}.",
                "",
                "Sem essas tags não dá para atribuir o custo a um projeto ou a um responsável.",
                f"A instância foi marcada com {COMPLIANCE_TAG} e continua rodando.",
            ]
        ),
    )
    logger.info(json.dumps({"instance": instance_id, "compliant": False, "missing": missing}))
    return {"instance": instance_id, "missing": missing}
