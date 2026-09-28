#!/bin/bash
cd ~/clustermotion

echo "Patching Makefile for fully automated destruction..."
sed -i.bak 's/terraform destroy/terraform destroy -auto-approve/g' Makefile

echo "1/2 Destroying EKS Clusters & Shared Infrastructure (This takes ~15-20 mins)..."
# This cleanly deletes Green, Blue, and all Shared infrastructure (including the EC2 management node)
make destroy-all
mv Makefile.bak Makefile

BUCKET="clustermotion-tfstate"
echo "2/2 Emptying and Deleting versioned Terraform state bucket: $BUCKET..."
# S3 won't let you delete a versioned bucket without deleting all historical versions first
aws s3api list-object-versions --bucket $BUCKET --query="Versions[].[Key,VersionId]" --output text 2>/dev/null | while read -r KEY VER; do
  if [ -n "$KEY" ] && [ "$KEY" != "None" ]; then
    aws s3api delete-object --bucket $BUCKET --key "$KEY" --version-id "$VER" >/dev/null
  fi
done

# Delete all historical delete markers
aws s3api list-object-versions --bucket $BUCKET --query="DeleteMarkers[].[Key,VersionId]" --output text 2>/dev/null | while read -r KEY VER; do
  if [ -n "$KEY" ] && [ "$KEY" != "None" ]; then
    aws s3api delete-object --bucket $BUCKET --key "$KEY" --version-id "$VER" >/dev/null
  fi
done

# Finally, delete the emptied state bucket
aws s3 rb s3://$BUCKET --force
echo "✅ Completely destroyed all infrastructure, ALBs, EKS clusters, and state buckets!"
