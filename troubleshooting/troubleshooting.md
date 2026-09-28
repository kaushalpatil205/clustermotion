# Troubleshooting & Problem Solving Log

During the setup and migration phases of this project, several blockers occurred. Here is how they were systematically solved.

---

## Issue 1: Boto3 Region Errors in External-DNS
**The Problem:** 
While spinning up the EKS clusters, the `external-dns` pod was crashlooping with errors stating that the AWS Region could not be found. This happens because Python's `boto3` library requires explicit region configuration when running inside certain pod environments without direct instance metadata access.

**The Solution:** 
We patched the ArgoCD Helm chart values for `external-dns` to explicitly inject the environment variables `AWS_DEFAULT_REGION` and `AWS_REGION` into the pods. By setting this to `us-east-1`, `boto3` was able to authenticate and update Route53 correctly.

**Commands used:**
```bash
# We modified the infra/gitops/system/external-dns.yaml manifest file.
# Then we manually applied the fix to the management cluster:
kubectl apply -f infra/gitops/system/external-dns.yaml --context cm-mgmt

# We then forced the deployment to restart so the new pods would pick up the environment variables:
kubectl rollout restart deployment external-dns -n kube-system
```

---

## Issue 2: `cm verify` KeyError on 'order_id'
**The Problem:** 
When proving the Zero-Downtime Migration, we ran the verification step (`cm verify --confirmed results/confirmed.jsonl`). However, the script crashed with a `KeyError: 'order_id'`. This occurred because the `k6` load testing tool's output JSON occasionally missed the `order_id` in its summary output rows due to edge-case logging omissions on aborted connections.

**The Solution:** 
We SSH'd directly into the Bastion Management node and hotfixed the Python engine's code (`verify.py`). We updated the script to safely extract the key using `.get("order_id")` instead of strict bracket notation, allowing it to bypass malformed log rows without crashing.

**Commands used:**
```bash
# SSH into the Bastion node
ssh -i ~/.ssh/id_ed25519 ubuntu@23.23.49.148

# We edited the engine file directly on the node using vim:
vim /opt/cm-venv/lib/python3.12/site-packages/clustermotion/verify.py

# Then we re-ran the verification command using cm:
export CM_CONFIG=/opt/clustermotion/engine/config.json
cm verify --confirmed results/confirmed.jsonl --json-out results/verify.json
```
