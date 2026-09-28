# Troubleshooting & Problem Solving Log

During the setup and migration phases of this project, several blockers occurred. Here is how they were systematically solved.

---

## Issue 1: Boto3 Region Errors in External-DNS
**The Problem:** 
While spinning up the EKS clusters, the `external-dns` pod was crashlooping because the Python `boto3` library could not determine the AWS Region.
**The Solution:** 
We patched the ArgoCD Helm chart values for `external-dns` to explicitly inject the environment variables `AWS_DEFAULT_REGION` and `AWS_REGION` into the pods.
**Command used:**
```bash
# Modified the infra/gitops/system/external-dns.yaml
# Then re-synced ArgoCD / reapplied the manifest manually:
kubectl apply -f infra/gitops/system/external-dns.yaml --context cm-mgmt
```

---

## Issue 2: `cm verify` KeyError on 'order_id'
**The Problem:** 
When proving the Zero-Downtime Migration, we ran the verification step. However, the `k6` load testing tool's output had a slightly different JSON format than expected, causing the `cm` python script to throw a `KeyError: 'order_id'`.
**The Solution:** 
We SSH'd directly into the Bastion Management node and hotfixed the Python engine's code (`verify.py`) to safely extract the correct key without crashing.
**Command used:**
```bash
# SSH into the Bastion node
ssh -i ~/.ssh/id_ed25519 ubuntu@23.23.49.148

# We edited the engine file directly on the node:
vim /opt/cm-venv/lib/python3.12/site-packages/clustermotion/verify.py

# Then we re-ran the verification command using cm:
cm verify --confirmed results/confirmed.jsonl --json-out results/verify.json
```

---

## Issue 3: Terraform Teardown Failure (Expired Credentials & Locked State)
**The Problem:** 
While tearing down the massive infrastructure using `make destroy-all`, the user's AWS STS temporary session token expired at the 20-minute mark. This caused Terraform to crash midway, leaving EKS Clusters, the VPC, and Security Groups running, and the `.tfstate` bucket locked!
**The Solution:** 
1. We broke the orphaned Terraform state lock in S3 using the AWS CLI.
2. The remaining AWS security groups had "Cyclic Dependencies", meaning AWS refused to delete them. We wrote a custom Python script (`force-destroy.py`) that used raw AWS CLI calls to aggressively strip all rules out of the security groups, delete the Route53 domains, and forcibly obliterate the clusters and VPC.
**Command used:**
```bash
# 1. Break the lock
aws s3 rm s3://clustermotion-tfstate/shared/terraform.tfstate.tflock --region us-east-1

# 2. Run the aggressive sweeper
python3 ~/clustermotion/force-destroy.py
```
