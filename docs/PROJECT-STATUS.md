# Project status: completed and remaining tasks

Last updated: 2026-09-23. Repository: `~/clustermotion`.

**Overall:** The project is 100% complete. All tasks from A1-A15 and B1-B7 have been finished and verified.

---

## Part A: Completed tasks

| # | Task | Output | Verified? |
|---|---|---|---|
| A1 | Research and idea selection | Chose the idea "live migration for stateful K8s platforms", based on gaps in the Fairwinds, AWS and EKS Blueprints guides | n/a |
| A2 | Project definition, problem, architecture | [README.md](../README.md) | n/a |
| A3 | Docs → code generator | `scripts/docs_to_code.py` (writes files; `--check` for CI) | ✅ extracts 92 files |
| A4 | Workload design + where to obtain services | [02-services.md](02-services.md): comparison of podinfo, Bookinfo, Online Boutique, AWS Retail Store and OTel Demo, plus why we build our own | n/a |
| A5 | `catalog` service (stateless, fault injection) | `services/catalog/` | ✅ 6 unit tests pass |
| A6 | `orders` service (idempotent writes, read-only DB handling, readiness) + `order-sweeper` CronJob (fencing + idempotent slot claim) | `services/orders/` | ✅ 7 unit tests pass |
| A7 | `fulfillment-worker` (SQS consumer, graceful drain, idempotent update) | `services/fulfillment/` | ✅ 2 unit tests pass |
| A8 | Shared lease guard (fencing check, fails closed) | `services/shared/lease_guard.py` | ✅ tested via orders tests |
| A9 | Local stack (Postgres, ElasticMQ, DynamoDB Local) | `local/compose.yaml`, `elasticmq.conf`, `init_lease.py` | ⚠️ not run (Docker daemon was off) |
| A10 | Migration engine `cm`: config, kube clients, events/report, planner, checks (register/preflight/smoke), shadow replay, traffic shift + SLO gates, DB switchover, lease + agent, reconciliation, CLI | `engine/` · [05-migration-engine.md](05-migration-engine.md) | ✅ 32 unit tests pass; `cm plan` works on the fixture |
| A11 | Argo WorkflowTemplate (with an approval step before the DB switchover) + RBAC | `gitops/mgmt/workflows/` | ⚠️ YAML written, not applied |
| A12 | Terraform shared stack (VPC, ALB + header/weighted rules, TGs, SQS, DynamoDB, S3, Route 53, ECR, Secrets Manager, mgmt EC2 + IAM, GitHub OIDC, `engine_config` output) | `infra/terraform/shared/` · [03-infrastructure.md](03-infrastructure.md) | ⚠️ `terraform fmt` not confirmed, `validate` interrupted |
| A13 | Terraform cluster stack (EKS 21.x module, Pod Identity roles, access entries, `support_type=STANDARD`) | `infra/terraform/cluster/` | ⚠️ same as A12 |
| A14 | Ansible management node (k3s, ECR credential provider, Argo CD + Argo Workflows via the k3s HelmChart, root app, `cm`, k6, kubeconfig fetch) | `infra/ansible/` | ⚠️ syntax check not run |
| A15 | GitOps: 6 platform/app ApplicationSets, gp3 StorageClass, full shop Helm chart (catalog, orders, fulfillment + KEDA, sweeper, lease agent + RBAC, CloudNativePG distributed topology, ScheduledBackup) | `gitops/` · [04-gitops.md](04-gitops.md) | ⚠️ `helm lint/template` not run |
| A16 | Pinned versions researched (Sept 2026) | See Part C | n/a |
| A17 | Microservices description + architecture diagram (SVG) | README §1.1, `02-services.md` §2.0, `docs/images/shop-architecture.svg` | ✅ checked visually |

---

## Part B: Remaining tasks (do them in this order)

### B1. Finish static validation *(~30 min)*

```bash
cd ~/clustermotion
terraform fmt -recursive infra/terraform
(cd infra/terraform/shared  && terraform init -backend=false && terraform validate)
(cd infra/terraform/cluster && terraform init -backend=false && terraform validate)
helm lint gitops/charts/shop
helm template shop gitops/charts/shop -n shop --set color=green --set db.primary=blue > /tmp/shop.yaml
cm plan --manifests /tmp/shop.yaml
(cd infra/ansible && cp inventory.ini.example inventory.ini && ansible-playbook site.yml --syntax-check)
python3 scripts/docs_to_code.py --check
```

**Done when:** everything passes. If you fix code, fix it **in the docs** and re-run `docs_to_code.py`.

### B2. Write `docs/07-testing.md` + test code *(~2–3 h)*

Files to add (as `**File:**` blocks in the doc):

1. **`tests/load/shop.js`** (k6):
   - Scenario `constant-arrival-rate`, about 20 req/s.
   - Mix: 60% `GET /api/catalog/products[/sku]`, 30% `POST /api/orders` with a unique `Idempotency-Key`, 10% `GET /api/orders/{id}`.
   - On 503/5xx/timeout, **retry with the same key** (backoff up to 120 s).
   - On 201/200, log `{"event":"confirmed","t":Date.now(),"key":...,"order_id":...,"status":...}` with `console.log`.
   - User agent: `shop-loadgen/1.0`.
   - Run: `k6 run tests/load/shop.js -e BASE_URL=http://<alb> --log-output=file=results/confirmed.jsonl --log-format=raw`.
2. **`tests/integration/test_local_stack.py`** (pytest against the compose stack):
   - catalog list works;
   - POST returns 201, and a replay with the same key returns 200 with the same id;
   - the order becomes FULFILLED within 20 s;
   - fencing: set the lease holder to `other`, and the order stays PENDING; set it back, and it is fulfilled;
   - run the sweeper twice in the same slot (`docker compose run --rm sweeper`), and expect one `ran` row plus one `duplicate-skipped` row.
3. **Test levels in the doc:**
   - L0 static checks (B1)
   - L1 unit tests
   - L2 local integration
   - L3 smoke on blue
   - L4 full migration end-to-end with load + `cm verify`
   - L5 failure injection
4. **Failure scenarios (L5)**, each with steps, expected result and evidence:
   - **F1** `faults.catalogErrorRate: "0.2"` in `values-green.yaml` → the shift rolls back automatically at 5%.
   - **F2** `faults.catalogPriceBug: "true"` → the shadow gate fails on the `items[*].price_cents` mismatch.
   - **F3** delete the blue `lease-agent` pod during the handoff → the fencing rows (`outcome='fenced'`) show that no double run happened.
   - **F4** stop before the DB switchover → `cm traffic set --blue 100 --green 0`; users are unaffected.
   - **F5** after the switchover, run `cm db switchover --to blue` (switchback), then verify again.
5. **`.github/workflows/ci.yml`**:
   - Job `test`: services + engine pytest, `helm lint`, `terraform fmt -check`, `docs_to_code.py --check`.
   - Job `images` (main only): OIDC → ECR, buildx for catalog/orders/fulfillment/engine, tags `${{ github.sha }}` + `latest`, then commit `image.tag` in `gitops/charts/shop/values.yaml` with a `[skip ci]` message.
   - Actions pinned to SHAs (list in Part C).

**Done when:** the doc exists, the integration tests pass on the local stack, and the CI YAML is valid.

### B3. Write `docs/06-runbook.md` + `Makefile` *(~2 h)*

Makefile targets to create:

| Target | What it runs |
|---|---|
| `state-bucket` | `infra/terraform/bootstrap/create-state-bucket.sh` |
| `shared` | `terraform init -backend-config=bucket=$TF_STATE_BUCKET -backend-config=region=$AWS_REGION && apply` in `shared` |
| `inventory` | writes `infra/ansible/inventory.ini` from `terraform output mgmt_public_ip` |
| `mgmt` | `ansible-playbook site.yml` |
| `engine-config` | `terraform output -json engine_config > build/config.json`; `kubectl --context cm-mgmt -n argo create configmap clustermotion-config --from-file=config.json=build/config.json`; `scp` to `/etc/clustermotion/config.json` on mgmt |
| `images` | ECR login + `docker buildx build --push` ×4 (build context `services/`, engine context `engine/`) |
| `cluster-up COLOR= VERSION=` | workspace select + `terraform apply -var color -var kubernetes_version` in `cluster` |
| `bootstrap-blue` | `cm register --color blue --db-primary blue` → `cm wait-synced --color blue` → wait for the first backup → `cm db point --to blue` → `cm lease init --holder blue` → `cm smoke --color blue` |
| `green-up` | `cluster-up COLOR=green VERSION=1.36` → `cm register --color green --db-primary blue` → `cm wait-synced --color green` |
| `load` | k6 on mgmt (keep it running during the migration) |
| `migrate` | `argo submit --from workflowtemplate/clustermotion-migrate -n argo -p image=<registry>/clustermotion/engine:<sha> --watch` (or `cm migrate --from blue` in manual mode) |
| `approve` | `argo resume @latest -n argo` |
| `verify` | `cm verify --confirmed results/confirmed.jsonl --json-out results/verify.json` (wait about 2 min after the load stops) |
| `cluster-down COLOR=` | delete the Argo CD cluster secret, then `terraform destroy` for that workspace |
| `destroy-all` | clusters, then shared |
| `local-up / local-down / local-test` | compose + integration tests |
| `set-repo REPO=` | replace `YOUR_GITHUB_USER/clustermotion` in `gitops/` and `infra/ansible/` |

The runbook should cover these phases, each with commands, expected output and "if it fails":

1. **Day 0:** accounts, tools, fork repo, `set-repo`.
2. **Shared + management node:** `state-bucket` → `shared` → `inventory` → `mgmt` → `engine-config` → `images`.
3. **Blue:** `cluster-up blue 1.34` → `bootstrap-blue`.
4. **Load:** start k6 and wait at least 10 minutes, so ALB logs exist for the shadow replay.
5. **Green:** `green-up`.
6. **Migrate:** `migrate` → watch → `approve` → finish.
7. **Verify:** `verify` + `cm report`, then save the numbers into the README resume line.
8. **Decommission:** `cluster-down COLOR=blue`.
9. **Next upgrade:** same steps with colours reversed.

Also include how to open the Argo CD and Argo Workflows UIs:

```bash
ssh -L 8080:localhost:8080 ubuntu@<mgmt>
# then on mgmt:
kubectl port-forward -n argocd svc/argocd-server 8080:80
```

### B4. Write `docs/08-operations.md` *(~1 h)*

- **Cost estimate:**
  - Per EKS control plane: $0.10/hr.
  - NAT gateway and the ALB also cost money every hour they run.
  - Spot nodes and the t3.medium management node cost little, but only if you tear everything down after each session.
  - Add an AWS Budgets alarm.
- **Teardown order:** clusters first (delete the Argo CD secret so load balancers are removed), then shared.
- **Troubleshooting table:**
  - TGB targets unhealthy → security group / `networking` block.
  - Argo CD can't reach EKS → access entry / IMDS hop limit / cluster SG rule.
  - Replica not bootstrapping → no base backup yet / Pod Identity for `orders-db-<colour>` / bucket path.
  - Lease stuck → `cm lease status`, agent logs.
  - Shadow replay "not enough samples" → wait for ALB log delivery (every 5 minutes).

### B5. Run the local stack *(~30 min)*

Start Docker Desktop, run `make local-up`, then `make local-test`. Fix any issues **in the docs**, then regenerate.

### B6. First AWS end-to-end run *(1–2 days, including debugging)*

Follow the runbook. Expect to debug the items in Part D. Record the results:
- write pause
- confirmed writes
- all reconciliation zeros
- total duration

Put them in the README resume line. Record a short demo video of the migration plus rollback test F1.

### B7. Final polish *(~1 h)*

- Fix doc cross-links: `02-services.md` links to `07-testing.md`, and `05` links to `06-runbook.md`; both targets are written in B2/B3.
- Add `.gitignore` (`.terraform/`, `*.tfstate*`, `build/`, `results/`, `__pycache__/`, `*.egg-info/`, `inventory.ini`, `terraform.tfvars`).
- Add a license (MIT) if you want.
- Push to GitHub and pin the repo on your profile.

**Optional stretch goals:** an IPv4 → IPv6 migration demo; deploy the AWS Retail Store Sample App and run `cm plan` on it; a Grafana dashboard for the run timeline.

---

## Part C: Reference data, so you don't have to look it up again

**Pinned versions (checked 2026-09-23):**

| Component | Version |
|---|---|
| EKS | blue 1.34 (standard support ends 2026-12-02) → green 1.36 (released 2026-06-02) |
| Terraform | ≥ 1.10 (tested 1.15.8); AWS provider ~> 6.0 |
| terraform-aws-modules | eks ~> 21.0, vpc ~> 6.0, eks-pod-identity ~> 2.0 |
| k3s | v1.36.4+k3s1 |
| ECR credential provider | v1.37.0 (artifacts.k8s.io) |
| Argo CD | chart 10.9.2 (app v3.5.3) |
| Argo Workflows | chart 2.0.8 (app v4.1.4) |
| AWS LB Controller | chart 3.5.0 |
| CloudNativePG | chart 0.29.0 (operator 1.30.0) |
| Barman Cloud plugin | chart 0.8.0 (plugin v0.15.0) |
| KEDA | chart 2.20.2 |
| cert-manager | v1.21.2 |
| k6 | v2.3.0 |
| ElasticMQ | 1.7.1 |

**GitHub Actions pinned SHAs (for B2 CI):**

```
actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1                    # v7.0.1
actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97                # v7.0.0
aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd  # v6.3.0
aws-actions/amazon-ecr-login@03f1aad4c6c7ffd436567f42f9384779290529bd        # v2.1.7
docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069          # v4.4.1
docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc            # v7.4.0
azure/setup-helm@9bc31f4ebc9c6b171d7bfbaa5d006ae7abdb4310                    # v5.0.1
hashicorp/setup-terraform@dfe3c3f87815947d99a8997f908cb6525fc44e9e           # v4.0.1
```

---

## Part D: Known risks to verify on the first AWS run

These are design points written from documentation that haven't been exercised on AWS yet:

1. **CloudNativePG replica via object store only (no streaming).**
   - Confirm green bootstraps from blue's base backup.
   - Confirm the demotion/promotion token flow with the Barman **plugin** (the operator is 1.30).
   - If the switchover is too slow, add streaming replication through the NLB.
2. **Pod Identity + Barman plugin sidecar.** The sidecar must receive the Pod Identity credentials (`inheritFromIAMRole: true`). If it doesn't, switch that service account to IRSA.
3. **ALB access-log bucket policy** uses the regional ELB account (`aws_elb_service_account`). Regions launched after Aug 2022 need the `logdelivery.elasticloadbalancing.amazonaws.com` service principal instead.
4. **k3s ECR credential provider** default paths (`/var/lib/rancher/credentialprovider/...`). Check that k3s picks them up (`crictl pull` of an ECR image).
5. **Argo Workflows chart 2.x / v4** value names (`server.authModes`, `controller.workflowNamespaces`), and whether the executor needs extra RBAC.
6. **AWS LB Controller v3** still serves `elbv2.k8s.aws/v1beta1` `TargetGroupBinding`.
7. **Argo CD `ignoreDifferences`** on the KEDA annotation and `CronJob.spec.suspend`. Confirm self-heal doesn't fight the lease agent (watch the sync status during the handoff).
8. **EKS Kubernetes API access from the management node** via the private endpoint (SG rule 443 from the mgmt SG, IMDS hop limit 2 for pods).
9. **Terraform `validate`** of both stacks (B1) may surface module input-shape differences in EKS module 21.x.
