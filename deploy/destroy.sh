#!/bin/bash
# Tear down everything provision.sh created, so nothing keeps billing.
#   bash deploy/destroy.sh <instance-id> <allocation-id> <sg-id> [name]
set -euo pipefail

IID="${1:?instance id}"
ALLOC="${2:?allocation id}"
SG="${3:?security group id}"
NAME="${4:-clipeditor}"
REGION="${REGION:-ap-south-1}"
aws_() { aws --region "$REGION" "$@"; }

echo "==> terminating $IID"
aws_ ec2 terminate-instances --instance-ids "$IID" >/dev/null
aws_ ec2 wait instance-terminated --instance-ids "$IID"

echo "==> releasing elastic IP $ALLOC"
aws_ ec2 release-address --allocation-id "$ALLOC" || true

echo "==> deleting key pair + security group"
aws_ ec2 delete-key-pair --key-name "$NAME" || true
# the SG can't be deleted until the ENI is fully gone; retry briefly
for i in $(seq 1 10); do
  aws_ ec2 delete-security-group --group-id "$SG" 2>/dev/null && break || sleep 10
done

echo "==> done. Nothing left billing."
