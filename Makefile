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
