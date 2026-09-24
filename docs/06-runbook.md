# Runbook: ClusterMotion migration from blue to green

This document walks through every step of a ClusterMotion migration, from initial setup to decommission.

> **Prerequisites:** AWS CLI configured, Terraform ≥ 1.10, Ansible, Helm, kubectl, k6, Argo CLI.

---

## Makefile

Every command below is wrapped in a Makefile target.  Run `make <target>` from the repo root.

**File:** `Makefile`

```makefile
.PHONY: state-bucket shared inventory mgmt engine-config images \
        cluster-up bootstrap-blue green-up load migrate approve verify \
        cluster-down destroy-all local-up local-down local-test set-repo

PROJECT     := clustermotion
AWS_REGION  ?= us-east-1
TF_STATE_BUCKET ?= $(PROJECT)-tfstate
REGISTRY    ?= $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null).dkr.ecr.$(AWS_REGION).amazonaws.com/$(PROJECT)
SHA         ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

state-bucket:
	bash infra/terraform/bootstrap/create-state-bucket.sh

shared:
	cd infra/terraform/shared && \
	  terraform init \
	    -backend-config=bucket=$(TF_STATE_BUCKET) \
	    -backend-config=region=$(AWS_REGION) && \
	  terraform apply

inventory:
	@cd infra/terraform/shared && \
	  echo "[mgmt]" > ../../../infra/ansible/inventory.ini && \
	  printf "%s ansible_user=ubuntu\n" \
	    "$$(terraform output -raw mgmt_public_ip)" \
	    >> ../../../infra/ansible/inventory.ini
	@echo "Wrote infra/ansible/inventory.ini"

mgmt:
	cd infra/ansible && ansible-playbook site.yml

engine-config:
	@mkdir -p build
	cd infra/terraform/shared && \
	  terraform output -json engine_config > ../../../build/config.json
	kubectl --context cm-mgmt -n argo create configmap clustermotion-config \
	  --from-file=config.json=build/config.json \
	  --dry-run=client -o yaml | kubectl apply -f -
	@echo "Config map applied to cm-mgmt"

# ---------------------------------------------------------------------------
# Container images
# ---------------------------------------------------------------------------

images:
	aws ecr get-login-password --region $(AWS_REGION) | \
	  docker login --username AWS --password-stdin \
	  $$(echo $(REGISTRY) | cut -d/ -f1)
	for svc in catalog orders fulfillment; do \
	  docker buildx build --push \
	    -t $(REGISTRY)/$$svc:$(SHA) \
	    -t $(REGISTRY)/$$svc:latest \
	    services/$$svc; \
	done
	docker buildx build --push \
	  -t $(REGISTRY)/engine:$(SHA) \
	  -t $(REGISTRY)/engine:latest \
	  engine

# ---------------------------------------------------------------------------
# EKS clusters
# ---------------------------------------------------------------------------

cluster-up:
	@test -n "$(COLOR)"   || { echo "Usage: make cluster-up COLOR=blue VERSION=1.34"; exit 1; }
	@test -n "$(VERSION)" || { echo "Usage: make cluster-up COLOR=blue VERSION=1.34"; exit 1; }
	cd infra/terraform/cluster && \
	  terraform workspace select -or-create $(COLOR) && \
	  terraform init \
	    -backend-config=bucket=$(TF_STATE_BUCKET) \
	    -backend-config=region=$(AWS_REGION) && \
	  terraform apply -var="color=$(COLOR)" -var="kubernetes_version=$(VERSION)"

bootstrap-blue:
	cm register --color blue --db-primary blue
	cm wait-synced --color blue
	@echo "Waiting for first ScheduledBackup to complete (~2 min)..."
	sleep 120
	cm db point --to blue
	cm lease init --holder blue
	cm smoke --color blue
	@echo "Blue cluster bootstrapped successfully."

green-up:
	$(MAKE) cluster-up COLOR=green VERSION=1.36
	cm register --color green --db-primary blue
	cm wait-synced --color green
	@echo "Green cluster ready. DB replica streaming from blue."

# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

MGMT_IP = $(shell cd infra/terraform/shared && terraform output -raw mgmt_public_ip 2>/dev/null)

load:
	@echo "Starting k6 load on management node (keep this running)..."
	ssh ubuntu@$(MGMT_IP) 'k6 run /opt/clustermotion/tests/load/shop.js \
	  -e BASE_URL=http://$$(cd /opt/clustermotion/infra/terraform/shared && terraform output -raw alb_dns) \
	  -e DURATION=60m \
	  --log-output=file=results/confirmed.jsonl --log-format=raw'

migrate:
	@test -n "$(IMAGE)" || { echo "Usage: make migrate IMAGE=<registry>/engine:<sha>"; exit 1; }
	argo submit --from workflowtemplate/clustermotion-migrate \
	  -n argo -p image=$(IMAGE) --watch

approve:
	argo resume @latest -n argo

verify:
	@echo "Waiting 2 minutes for in-flight processing to drain..."
	sleep 120
	cm verify --confirmed results/confirmed.jsonl --json-out results/verify.json
	cm report
	@echo "Check results/verify.json for reconciliation details."

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

cluster-down:
	@test -n "$(COLOR)" || { echo "Usage: make cluster-down COLOR=blue"; exit 1; }
	kubectl --context cm-mgmt -n argocd delete secret cm-$(COLOR) --ignore-not-found
	@echo "Waiting 30 s for controllers to clean up..."
	sleep 30
	cd infra/terraform/cluster && \
	  terraform workspace select $(COLOR) && \
	  terraform destroy

destroy-all:
	-$(MAKE) cluster-down COLOR=green
	-$(MAKE) cluster-down COLOR=blue
	cd infra/terraform/shared && terraform destroy

# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------

local-up:
	docker compose -f local/compose.yaml up --build -d
	@echo "Waiting for services to start..."
	sleep 10

local-down:
	docker compose -f local/compose.yaml down -v

local-test:
	pytest tests/integration/test_local_stack.py -v

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

set-repo:
	@test -n "$(REPO)" || { echo "Usage: make set-repo REPO=you/clustermotion"; exit 1; }
	find gitops/ infra/ansible/ -type f \( -name '*.yaml' -o -name '*.yml' -o -name '*.j2' \) | \
	  xargs sed -i'' -e 's|YOUR_GITHUB_USER/clustermotion|$(REPO)|g'
	@echo "Repository references updated to $(REPO)"
```

---

## Phase 1 — Day 0: prerequisites

| Item | Check |
|---|---|
| AWS account with admin access | `aws sts get-caller-identity` |
| Tools installed | `terraform version`, `ansible --version`, `helm version`, `kubectl version --client`, `k6 version`, `argo version` |
| Repo cloned and `set-repo` run | `make set-repo REPO=<your-github-user>/clustermotion` |
| SSH key pair for the management node | A key pair in `~/.ssh/` (the Terraform `shared` stack references `var.ssh_public_key`) |

---

## Phase 2 — Shared infrastructure + management node

```bash
make state-bucket          # creates the S3 bucket for Terraform state
make shared                # VPC, ALB, SQS, DynamoDB, S3, Route 53, ECR, mgmt EC2
make inventory             # writes Ansible inventory from Terraform output
make mgmt                  # installs k3s, Argo CD, Argo Workflows, cm, k6
make engine-config         # pushes engine_config.json to mgmt and k8s configmap
make images                # builds + pushes 4 Docker images to ECR
```

**Expected:** all commands exit 0.  The management node is reachable via SSH.

**If it fails:**
- `shared` — check AWS credentials, region, quota limits.
- `mgmt` — check SSH connectivity (`ssh ubuntu@<mgmt-ip>`), verify security group allows port 22.
- `images` — check ECR login, Docker daemon running.

---

## Phase 3 — Blue cluster (source, EKS 1.34)

```bash
make cluster-up COLOR=blue VERSION=1.34
make bootstrap-blue
```

**Expected:** EKS cluster is up, Argo CD deploys all workloads, DB primary is on blue, lease holder is blue, smoke tests pass.

**If it fails:**
- Check Argo CD sync status: `kubectl --context cm-mgmt -n argocd get app`
- Check pods: `kubectl --context cm-blue -n shop get pods`
- DB not ready: wait for ScheduledBackup, check Pod Identity.

---

## Phase 4 — Start load test

```bash
make load      # run in a separate terminal; keep it running
```

Wait **at least 10 minutes** before proceeding — ALB access logs are delivered every 5 minutes, and the shadow replay needs at least 2 batches.

---

## Phase 5 — Green cluster (target, EKS 1.36)

```bash
make green-up
```

**Expected:** EKS 1.36 cluster is up, Argo CD deploys everything, DB replica is streaming WAL from blue via S3, singletons are paused (lease is still on blue).

---

## Phase 6 — Migrate

### Option A: Argo Workflow (recommended)

```bash
make migrate IMAGE=$REGISTRY/engine:$SHA
```

Watch the Argo Workflows UI.  The workflow will:
1. Run preflight checks
2. Run smoke tests on green (header-routed)
3. Replay shadow traffic and diff responses
4. Shift traffic progressively: 5 % → 25 % → 50 % → 100 %
5. **Suspend** — waiting for your approval before the DB switchover

```bash
make approve        # approve the DB switchover
```

The workflow then:
6. Demotes blue DB → extracts promotion token → promotes green → flips Route 53
7. Hands the lease from blue to green (fulfillment-worker + order-sweeper switch clusters)
8. Runs post-checks

### Option B: manual mode

```bash
cm migrate --from blue
```

This runs all steps sequentially and pauses for confirmation before the DB switchover.

**If SLO gate fails:** traffic rolls back to blue = 100 % automatically.  Fix green and retry.

---

## Phase 7 — Verify

Stop the k6 load test (Ctrl+C), then:

```bash
make verify
```

**Expected output:**

```
lost_writes:      0
duplicate_keys:   0
double_fulfilled: 0
missed_slots:     0
overlap_seconds:  0
write_pause:      Xs
```

Copy the `write_pause` and total request count into the README resume line.

---

## Phase 8 — Decommission blue

```bash
make cluster-down COLOR=blue
```

---

## Phase 9 — Next upgrade

Same steps with colours reversed.  Green is now the source; a new "blue" (or any name) at 1.38 is the target.

---

## Accessing the UIs

### Argo CD

```bash
ssh -L 8080:localhost:8080 ubuntu@<mgmt-ip>
# on the mgmt node:
kubectl port-forward -n argocd svc/argocd-server 8080:80
```

Open `http://localhost:8080`.  Username: `admin`.  Password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

### Argo Workflows

```bash
ssh -L 2746:localhost:2746 ubuntu@<mgmt-ip>
# on the mgmt node:
kubectl port-forward -n argo svc/argo-server 2746:2746
```

Open `https://localhost:2746`.
