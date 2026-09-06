#!/bin/bash
# One-shot AWS provisioner for the Clip Editor server.
#
#   bash deploy/provision.sh
#
# Needs: AWS CLI configured (aws configure), ssh, and a .env in the repo root
# holding ANTHROPIC_API_KEY + ELEVENLABS_API_KEY.
#
# Creates: elastic IP, security group, key pair, one EC2 instance.
# Prints:  the HTTPS URL and the password to share.
set -euo pipefail

REGION="${REGION:-ap-south-1}"          # Mumbai - closest to you
TYPE="${TYPE:-t3.small}"                # 2 GB RAM; t3.micro is half the cost but tight
DISK_GB="${DISK_GB:-20}"
NAME="${NAME:-clipeditor}"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/${NAME}.pem}"
BASIC_USER="${BASIC_USER:-hitesh}"
REPO_URL="${REPO_URL:-https://github.com/ZeptorAI/clipping-engine-for-hitesh.git}"
BRANCH="${BRANCH:-main}"

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
[ -f "$ROOT/.env" ] || { echo "!! no .env in $ROOT - create it first"; exit 1; }

say() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
aws_() { aws --region "$REGION" "$@"; }

say "Checking credentials"
aws_ sts get-caller-identity --query 'Account' --output text

# ---------- password ----------
PASS="${PASS:-$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)}"

# ---------- elastic IP (allocate first: the hostname is derived from it) ----------
say "Allocating a static IP"
ALLOC_ID=$(aws_ ec2 allocate-address --domain vpc \
           --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME}]" \
           --query AllocationId --output text)
IP=$(aws_ ec2 describe-addresses --allocation-ids "$ALLOC_ID" \
     --query 'Addresses[0].PublicIp' --output text)
SITE_HOST="${IP}.nip.io"     # resolves to $IP, so Caddy can get a real cert
echo "    IP        : $IP"
echo "    hostname  : $SITE_HOST"

# ---------- key pair ----------
say "Creating SSH key"
mkdir -p "$(dirname "$KEY_PATH")"
if [ -f "$KEY_PATH" ]; then
  echo "    reusing $KEY_PATH"
else
  aws_ ec2 create-key-pair --key-name "$NAME" \
    --query KeyMaterial --output text > "$KEY_PATH"
  chmod 600 "$KEY_PATH"
  echo "    saved $KEY_PATH"
fi

# ---------- security group ----------
say "Creating security group"
MYIP=$(curl -s https://checkip.amazonaws.com || echo "0.0.0.0")
VPC=$(aws_ ec2 describe-vpcs --filters Name=isDefault,Values=true \
      --query 'Vpcs[0].VpcId' --output text)
SG=$(aws_ ec2 create-security-group --group-name "$NAME" --vpc-id "$VPC" \
     --description "Clip Editor" --query GroupId --output text)
# 80/443 open to the world (Caddy needs 80 for the cert challenge; auth is at the
# proxy). SSH locked to the machine running this script.
aws_ ec2 authorize-security-group-ingress --group-id "$SG" \
  --ip-permissions \
    "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]" \
    "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]" \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${MYIP%$'\n'}/32,Description=admin}]" \
  >/dev/null
echo "    $SG  (ssh limited to ${MYIP%$'\n'})"

# ---------- launch ----------
say "Launching $TYPE"
# newest Ubuntu 24.04 published by Canonical (owner 099720109477).
# Direct query, not the SSM alias - the alias is not resolvable in every account.
AMI=$(aws_ ec2 describe-images --owners 099720109477 \
      --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
                "Name=state,Values=available" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
[ -n "$AMI" ] && [ "$AMI" != "None" ] || { echo "!! could not resolve an Ubuntu AMI"; exit 1; }
echo "    ami       : $AMI"
# inject config as exports right after the shebang (portable; no envsubst)
USERDATA=$( { head -1 "$HERE/bootstrap.sh";
              echo "export REPO_URL='$REPO_URL'";
              echo "export BRANCH='$BRANCH'";
              tail -n +2 "$HERE/bootstrap.sh"; } )
IID=$(aws_ ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" \
      --key-name "$NAME" --security-group-ids "$SG" \
      --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$DISK_GB,VolumeType=gp3}" \
      --user-data "$USERDATA" \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
      --query 'Instances[0].InstanceId' --output text)
echo "    $IID"
aws_ ec2 wait instance-running --instance-ids "$IID"
aws_ ec2 associate-address --instance-id "$IID" --allocation-id "$ALLOC_ID" >/dev/null
echo "    running, IP attached"

# ---------- wait for SSH + bootstrap ----------
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $KEY_PATH ubuntu@$IP"
say "Waiting for SSH"
for i in $(seq 1 40); do
  $SSH -o ConnectTimeout=5 true 2>/dev/null && break || sleep 10
done

say "Waiting for first-boot setup (installing ffmpeg, python, caddy - a few minutes)"
for i in $(seq 1 60); do
  if $SSH 'test -f /var/lib/clipeditor-bootstrap-done' 2>/dev/null; then
    echo "    done"; break
  fi
  sleep 15
done

# ---------- secrets + public site ----------
say "Uploading .env"
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$KEY_PATH" \
    "$ROOT/.env" "ubuntu@$IP:/opt/clipeditor/.env"
$SSH 'chmod 600 /opt/clipeditor/.env && sudo systemctl restart clipeditor'

say "Configuring HTTPS + password"
HASH=$($SSH "caddy hash-password --plaintext '$PASS'")
$SSH "sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
$SITE_HOST {
    basic_auth {
        $BASIC_USER $HASH
    }
    request_body {
        max_size 2GB
    }
    reverse_proxy 127.0.0.1:5000 {
        transport http {
            read_timeout 3600s
            write_timeout 3600s
        }
    }
}
EOF
sudo systemctl restart caddy"

cat <<DONE

============================================================
  Clip Editor is live

  URL       https://$SITE_HOST
  user      $BASIC_USER
  password  $PASS

  Reels     https://$SITE_HOST/
  Tighten   https://$SITE_HOST/tighten

  ssh       ssh -i $KEY_PATH ubuntu@$IP
  logs      $SSH 'journalctl -u clipeditor -f'
  stop      aws --region $REGION ec2 stop-instances --instance-ids $IID
  start     aws --region $REGION ec2 start-instances --instance-ids $IID
  destroy   bash deploy/destroy.sh $IID $ALLOC_ID $SG $NAME
============================================================

Save that password - it is not stored anywhere.
DONE
