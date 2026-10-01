import json
from datetime import UTC, datetime, timedelta

import boto3
from moto import mock_aws

import app


@mock_aws
def test_scan_finds_each_kind_of_waste():
    ec2 = boto3.client("ec2")
    logs = boto3.client("logs")

    ec2.create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp3")
    ec2.allocate_address(Domain="vpc")
    logs.create_log_group(logGroupName="/sem/retencao")
    logs.create_log_group(logGroupName="/com/retencao")
    logs.put_retention_policy(logGroupName="/com/retencao", retentionInDays=7)

    findings = app.scan(ec2, logs, snapshot_age_days=90, now=datetime.now(UTC))
    kinds = {f["type"] for f in findings}

    assert "ebs_volume_unattached" in kinds
    assert "elastic_ip_unassociated" in kinds
    assert [f["id"] for f in findings if f["type"] == "log_group_no_retention"] == ["/sem/retencao"]
    volume = next(f for f in findings if f["type"] == "ebs_volume_unattached")
    assert volume["monthly_usd"] == 0.8


@mock_aws
def test_snapshot_age_threshold():
    ec2 = boto3.client("ec2")
    vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=8)
    snap_id = ec2.create_snapshot(VolumeId=vol["VolumeId"])["SnapshotId"]
    now = datetime.now(UTC)

    # O moto cria snapshots das AMIs padrão na conta; compara só o deste teste.
    def ids(found):
        return {f["id"] for f in found}

    assert snap_id not in ids(app.old_snapshots(ec2, max_age_days=90, now=now))
    assert snap_id in ids(app.old_snapshots(ec2, max_age_days=90, now=now + timedelta(days=91)))


@mock_aws
def test_handler_writes_report_and_notifies(monkeypatch):
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="relatorios")
    topic = boto3.client("sns").create_topic(Name="alertas")["TopicArn"]
    boto3.client("ec2").create_volume(AvailabilityZone="us-east-1a", Size=5, VolumeType="gp2")
    monkeypatch.setenv("REPORT_BUCKET", "relatorios")
    monkeypatch.setenv("TOPIC_ARN", topic)

    result = app.handler({}, None)

    assert result["findings"] >= 1
    body = json.loads(s3.get_object(Bucket="relatorios", Key=result["report"])["Body"].read())
    assert body["monthly_usd_total"] == result["monthly_usd_total"]


@mock_aws
def test_clean_account_sends_no_email(monkeypatch):
    boto3.client("s3").create_bucket(Bucket="relatorios")
    monkeypatch.setenv("REPORT_BUCKET", "relatorios")
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:inexistente")

    # Publicar num tópico inexistente falharia; sem achados, não publica.
    result = app.handler({}, None)

    assert result["findings"] == 0
