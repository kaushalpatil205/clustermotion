# Kubernetes and Operations Commands

This document contains **every single command** executed from the start to the end of the project for setting up the infrastructure, installing ArgoCD, managing the clusters, and destroying the environment.

## 1. Initial Authentication & Tooling

First, we authenticated with AWS and ensured we had the required CLI tools:
```bash
# Configure AWS Credentials
aws configure
export AWS_DEFAULT_REGION="us-east-1"
export AWS_REGION="us-east-1"

# Install required cluster management tools (Mac/Homebrew)
brew install kubectl helm argocd awscli terraform k6
```

## 2. Infrastructure Setup (VPC & Bastion)

We used the project's Makefile to provision the networking and management node using Terraform:
```bash
# Initialize the backend S3 state bucket and provision the Management Node (Bastion)
make bootstrap
```

## 3. Provisioning the Blue Cluster (Origin)

We created the primary `cm-blue` EKS cluster and synced it with ArgoCD:
```bash
# Provision the Blue EKS Cluster and sync the ArgoCD GitOps application state
make cluster-up COLOR=blue

# Configure local kubectl to talk to the Bastion and Blue clusters
aws eks update-kubeconfig --name cm-blue --region us-east-1
```

## 4. ArgoCD Installation & Access

ArgoCD was installed on the Management Bastion node to handle GitOps deployments for both clusters:
```bash
# Set the current kubectl context to the Bastion Management Node
kubectl config use-context cm-mgmt

# Verify ArgoCD pods are running
kubectl get pods -n argocd

# Retrieve the initial admin password for the ArgoCD UI
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d

# Port-forward the ArgoCD server to access it locally on localhost:8080
kubectl port-forward svc/argocd-server -n argocd 8080:443

# Login to ArgoCD via CLI
argocd login localhost:8080 --username admin --password <password> --insecure
```

## 5. Provisioning the Green Cluster (Target)

With the Blue cluster handling live traffic, we spun up the Green cluster to prepare for migration:
```bash
# Provision the Green EKS Cluster and register it with the GitOps management plane
make cluster-up COLOR=green

# Verify the Green cluster's application pods are syncing
kubectl config use-context cm-green
kubectl get pods -n shop
```

## 6. Troubleshooting Overrides (During Setup)

During setup, `external-dns` failed to find the AWS region. We manually patched it:
```bash
# Applied a direct hotfix to inject AWS_DEFAULT_REGION into external-dns
kubectl apply -f infra/gitops/system/external-dns.yaml --context cm-mgmt

# Restarted the deployment to pick up the new environment variables
kubectl rollout restart deployment external-dns -n kube-system
```

## 7. Verifying the Migration

After using the `cm` python tool to perform the migration, we used `kubectl` to verify the state of the new database on the Green cluster:
```bash
# SSH into the Bastion node and check the PostgreSQL cluster status on Green
ssh -i ~/.ssh/id_ed25519 ubuntu@23.23.49.148 'KUBECONFIG=~/.kube/config kubectl --context arn:aws:eks:us-east-1:561789488706:cluster/cm-green -n shop get clusters.postgresql.cnpg.io,pods'
```

## 8. Total Infrastructure Destruction

To ensure no orphaned AWS resources were left behind, we ran the destruction sequence:

### A. The Standard Teardown
```bash
# Destroys EKS clusters, load balancers, and the shared VPC
make destroy-all
```

### B. The Aggressive Force-Destroy (Handling Expired Tokens)
Because the AWS session token expired mid-teardown during `make destroy-all`, the Terraform state locked and left "Cyclic Security Group Dependencies" stranded. We executed this exact sequence to force-destroy the remaining environment:

```bash
# 1. Export fresh AWS Credentials
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_SESSION_TOKEN="..."
export AWS_DEFAULT_REGION="us-east-1"

# 2. Break the orphaned Terraform state lock
aws s3 rm s3://clustermotion-tfstate/shared/terraform.tfstate.tflock

# 3. Run the custom Python cleanup script to obliterate EKS, ALBs, SGs, and the VPC
python3 ~/clustermotion/force-destroy.py
```
