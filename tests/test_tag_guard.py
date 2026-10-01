import boto3
import pytest
from moto import mock_aws

from conftest import load_function

app = load_function("tag_guard")


def running_event(instance_id):
    return {"region": "us-east-1", "detail": {"instance-id": instance_id, "state": "running"}}


@pytest.fixture
def env(monkeypatch):
    with mock_aws():
        topic = boto3.client("sns").create_topic(Name="alertas")["TopicArn"]
        monkeypatch.setenv("TOPIC_ARN", topic)
        monkeypatch.setenv("REQUIRED_TAGS", "Project,Environment,Owner")
        yield boto3.client("ec2")


def launch(ec2, tags):
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    spec = [{"ResourceType": "instance", "Tags": tags}] if tags else []
    return ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, InstanceType="t4g.nano", TagSpecifications=spec)[
        "Instances"
    ][0]["InstanceId"]


def test_missing_tags_ignores_blank_values():
    tags = [{"Key": "Project", "Value": "x"}, {"Key": "Owner", "Value": " "}]
    assert app.missing_tags(tags, ["Project", "Environment", "Owner"]) == ["Environment", "Owner"]


def test_untagged_instance_is_marked(env):
    instance_id = launch(env, [{"Key": "Project", "Value": "demo"}])

    result = app.handler(running_event(instance_id), None)

    assert result["missing"] == ["Environment", "Owner"]
    tags = env.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]["Tags"]
    assert {"Key": "TagCompliance", "Value": "faltando:Environment+Owner"} in tags


def test_compliant_instance_is_left_alone(env):
    tags = [{"Key": k, "Value": "x"} for k in ("Project", "Environment", "Owner")]
    instance_id = launch(env, tags)

    result = app.handler(running_event(instance_id), None)

    assert result["missing"] == []
    current = env.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]["Tags"]
    assert all(t["Key"] != "TagCompliance" for t in current)
