import boto3
import pytest
from moto import mock_aws

from conftest import load_function

app = load_function("office_hours")


@pytest.fixture
def ec2(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("SCHEDULE_TAG_VALUE", "office-hours")
        yield boto3.client("ec2")


def launch(ec2, tags):
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    spec = [{"ResourceType": "instance", "Tags": tags}]
    reservation = ec2.run_instances(ImageId=image, MinCount=1, MaxCount=1, TagSpecifications=spec)
    return reservation["Instances"][0]["InstanceId"]


def state(ec2, instance_id):
    return ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]["State"]["Name"]


def test_stop_only_touches_scheduled_instances(ec2):
    scheduled = launch(ec2, [{"Key": "Schedule", "Value": "office-hours"}])
    other = launch(ec2, [{"Key": "Project", "Value": "x"}])

    result = app.handler({"action": "stop"}, None)

    assert result["instances"] == [scheduled]
    assert state(ec2, scheduled) == "stopped"
    assert state(ec2, other) == "running"


def test_start_brings_scheduled_instances_back(ec2):
    scheduled = launch(ec2, [{"Key": "Schedule", "Value": "office-hours"}])
    ec2.stop_instances(InstanceIds=[scheduled])

    result = app.handler({"action": "start"}, None)

    assert result["instances"] == [scheduled]
    assert state(ec2, scheduled) == "running"


def test_nothing_to_do_is_not_an_error(ec2):
    assert app.handler({"action": "start"}, None) == {"action": "start", "instances": []}


def test_unknown_action_is_rejected(ec2):
    with pytest.raises(ValueError):
        app.handler({"action": "reboot"}, None)
