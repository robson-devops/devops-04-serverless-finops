"""Para e liga, por horário, as instâncias marcadas com a tag de agendamento."""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Ação pedida -> estado em que a instância precisa estar para ser afetada.
SOURCE_STATE = {"stop": "running", "start": "stopped"}


def scheduled_instances(ec2, tag_value, state):
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[
            {"Name": "tag:Schedule", "Values": [tag_value]},
            {"Name": "instance-state-name", "Values": [state]},
        ]
    )
    return [inst["InstanceId"] for page in pages for res in page["Reservations"] for inst in res["Instances"]]


def handler(event, context):
    action = event.get("action")
    if action not in SOURCE_STATE:
        raise ValueError(f"action deve ser stop ou start, recebido: {action!r}")

    ec2 = boto3.client("ec2")
    ids = scheduled_instances(ec2, os.environ["SCHEDULE_TAG_VALUE"], SOURCE_STATE[action])
    if ids:
        if action == "stop":
            ec2.stop_instances(InstanceIds=ids)
        else:
            ec2.start_instances(InstanceIds=ids)

    logger.info(json.dumps({"action": action, "instances": ids}))
    return {"action": action, "instances": ids}
