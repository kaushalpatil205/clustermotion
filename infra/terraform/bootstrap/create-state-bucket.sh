#!/usr/bin/env bash
# Creates the S3 bucket that stores Terraform state (versioned, encrypted, private).
set -euo pipefail
: "${TF_STATE_BUCKET:?set TF_STATE_BUCKET}"
: "${AWS_REGION:?set AWS_REGION}"

if aws s3api head-bucket --bucket "$TF_STATE_BUCKET" 2>/dev/null; then
  echo "bucket $TF_STATE_BUCKET already exists"; exit 0
fi
if [ "$AWS_REGION" = "us-east-1" ]; then
  aws s3api create-bucket --bucket "$TF_STATE_BUCKET" --region "$AWS_REGION"
else
  aws s3api create-bucket --bucket "$TF_STATE_BUCKET" --region "$AWS_REGION" \
    --create-bucket-configuration LocationConstraint="$AWS_REGION"
fi
aws s3api put-bucket-versioning --bucket "$TF_STATE_BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$TF_STATE_BUCKET" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "$TF_STATE_BUCKET" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
echo "created $TF_STATE_BUCKET"
