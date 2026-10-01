"""Procura recursos que geram custo sem uso e publica um relatório."""

import json
import logging
import os
from datetime import UTC, datetime, timedelta

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Preços de referência da us-east-1 (US$ por mês). Servem para ordenar e
# dimensionar o desperdício, não para conciliar com a fatura.
EBS_GB_MONTH = {"gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015, "standard": 0.05}
SNAPSHOT_GB_MONTH = 0.05
PUBLIC_IPV4_MONTH = 0.005 * 730
LOG_GB_MONTH = 0.03


def unattached_volumes(ec2):
    findings = []
    pages = ec2.get_paginator("describe_volumes").paginate(Filters=[{"Name": "status", "Values": ["available"]}])
    for page in pages:
        for vol in page["Volumes"]:
            price = EBS_GB_MONTH.get(vol["VolumeType"], 0.10)
            findings.append(
                {
                    "type": "ebs_volume_unattached",
                    "id": vol["VolumeId"],
                    "detail": f"{vol['Size']} GB {vol['VolumeType']}",
                    "monthly_usd": round(vol["Size"] * price, 2),
                }
            )
    return findings


def idle_elastic_ips(ec2):
    findings = []
    for addr in ec2.describe_addresses()["Addresses"]:
        if "AssociationId" not in addr:
            findings.append(
                {
                    "type": "elastic_ip_unassociated",
                    "id": addr.get("AllocationId", addr["PublicIp"]),
                    "detail": addr["PublicIp"],
                    "monthly_usd": round(PUBLIC_IPV4_MONTH, 2),
                }
            )
    return findings


def old_snapshots(ec2, max_age_days, now):
    findings = []
    limit = now - timedelta(days=max_age_days)
    pages = ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"])
    for page in pages:
        for snap in page["Snapshots"]:
            if snap["StartTime"] < limit:
                age = (now - snap["StartTime"]).days
                findings.append(
                    {
                        "type": "ebs_snapshot_old",
                        "id": snap["SnapshotId"],
                        "detail": f"{snap['VolumeSize']} GB, {age} dias",
                        # O tamanho do volume é o teto; snapshots são incrementais.
                        "monthly_usd": round(snap["VolumeSize"] * SNAPSHOT_GB_MONTH, 2),
                    }
                )
    return findings


def stopped_instances(ec2):
    findings = []
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]
    )
    volume_ids = []
    instances = []
    for page in pages:
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                ids = [m["Ebs"]["VolumeId"] for m in inst.get("BlockDeviceMappings", []) if "Ebs" in m]
                volume_ids.extend(ids)
                instances.append((inst, ids))
    sizes = {}
    if volume_ids:
        for vol in ec2.describe_volumes(VolumeIds=volume_ids)["Volumes"]:
            sizes[vol["VolumeId"]] = (vol["Size"], vol["VolumeType"])
    for inst, ids in instances:
        # Parada não cobra computação, mas os discos continuam cobrando.
        cost = sum(sizes[v][0] * EBS_GB_MONTH.get(sizes[v][1], 0.10) for v in ids if v in sizes)
        findings.append(
            {
                "type": "ec2_instance_stopped",
                "id": inst["InstanceId"],
                "detail": f"{inst['InstanceType']}, {len(ids)} volume(s)",
                "monthly_usd": round(cost, 2),
            }
        )
    return findings


def log_groups_without_retention(logs):
    findings = []
    for page in logs.get_paginator("describe_log_groups").paginate():
        for group in page["logGroups"]:
            if "retentionInDays" not in group:
                gb = group.get("storedBytes", 0) / 1e9
                findings.append(
                    {
                        "type": "log_group_no_retention",
                        "id": group["logGroupName"],
                        "detail": f"{gb:.3f} GB armazenados",
                        "monthly_usd": round(gb * LOG_GB_MONTH, 2),
                    }
                )
    return findings


def scan(ec2, logs, snapshot_age_days, now):
    findings = []
    findings += unattached_volumes(ec2)
    findings += idle_elastic_ips(ec2)
    findings += old_snapshots(ec2, snapshot_age_days, now)
    findings += stopped_instances(ec2)
    findings += log_groups_without_retention(logs)
    return sorted(findings, key=lambda f: f["monthly_usd"], reverse=True)


def summary(findings, region, report_key):
    total = round(sum(f["monthly_usd"] for f in findings), 2)
    subject = f"[FinOps] {len(findings)} item(ns) sem uso, ~US$ {total:.2f}/mês"
    lines = [f"Varredura de desperdício na região {region}.", ""]
    for f in findings:
        lines.append(f"- {f['type']}: {f['id']} ({f['detail']}) ~US$ {f['monthly_usd']:.2f}/mês")
    lines += ["", f"Relatório completo: {report_key}"]
    return subject, "\n".join(lines), total


def handler(event, context):
    region = os.environ.get("AWS_REGION", "us-east-1")
    bucket = os.environ["REPORT_BUCKET"]
    topic = os.environ["TOPIC_ARN"]
    age = int(os.environ.get("SNAPSHOT_AGE_DAYS", "90"))
    now = datetime.now(UTC)

    findings = scan(boto3.client("ec2"), boto3.client("logs"), age, now)
    key = f"reports/{now:%Y/%m/%d}/waste-{now:%H%M%S}.json"
    subject, message, total = summary(findings, region, f"s3://{bucket}/{key}")

    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(
            {"generated_at": now.isoformat(), "region": region, "monthly_usd_total": total, "findings": findings},
            indent=2,
        ),
        ContentType="application/json",
    )
    # Sem achados não há e-mail: aviso diário de "nada encontrado" vira ruído.
    if findings:
        boto3.client("sns").publish(TopicArn=topic, Subject=subject[:100], Message=message)

    logger.info(json.dumps({"findings": len(findings), "monthly_usd_total": total, "report": key}))
    return {"findings": len(findings), "monthly_usd_total": total, "report": key}
