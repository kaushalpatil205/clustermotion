# Operations

This document covers operational aspects of running the ClusterMotion project on AWS.

## 1. Cost Estimate

A single migration run takes a few hours. Tear down everything afterward to avoid ongoing costs.

| Resource | Unit cost | Notes |
|---|---|---|
| EKS control plane | $0.10/hr per cluster | ~$72/mo per cluster. During migration you run 2 clusters. |
| NAT Gateway | ~$0.045/hr + $0.045/GB | Needed for private subnets. |
| ALB | ~$0.0225/hr + LCU charges | Application Load Balancer costs. |
| EC2 mgmt node | ~$0.0416/hr (t3.medium) | ~$30/mo if left running. |
| EC2 worker nodes | Variable ($0.01-0.05/hr) | Using spot instances keeps this low. |
| S3 | Minimal (< $1/mo for demo) | Stores WAL archives, ALB logs, Terraform state, artifacts. |
| SQS | Free tier covers demo | Fulfillment worker queue. |
| DynamoDB | On-demand (< $1/mo for demo) | Lease and run tracking tables. |
| Route 53 zone | $0.50/mo | Internal DNS for database routing. |
| ECR | $0.10/GB/mo storage | Container images. |

> [!TIP]
> **Total cost for a demo run:** Roughly $20-40 if torn down within a few hours.
> Set up an AWS Budgets alarm at $50/day to prevent accidental overspend if you forget to run `make destroy-all`.

## 2. Teardown Order

**CRITICAL:** You must follow this exact order to avoid orphaned resources or hanging Terraform destroys:

1. **Delete Argo CD cluster secrets** (`kubectl --context cm-mgmt -n argocd delete secret cm-<color>`). This makes Argo CD delete the resources in the cluster, critically including the `TargetGroupBinding`s. If you don't do this, the AWS Load Balancer Controller won't detach the instances from the Target Groups.
2. **Wait 30s** for the controllers to clean up.
3. **Destroy cluster workspaces** (`terraform destroy` in the `cluster` directory for blue, then green).
4. **Destroy shared stack** (`terraform destroy` in the `shared` directory).
5. Manually check the AWS Console for any orphaned ENIs, EBS volumes, or load balancers.

> [!WARNING]
> If you destroy the `shared` stack before the `cluster` stacks, the ALB rules and target groups vanish, but the EKS node security groups may still reference them, causing `terraform destroy cluster` to hang indefinitely.

## 3. Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| TGB targets show unhealthy | Security group missing ingress rule from ALB SG, or wrong port in the networking block of the `TargetGroupBinding`. | Verify the `shared` module output `cluster_security_group_id` is attached to the ALB. |
| Argo CD can't reach EKS API | Access entry not created for mgmt role, IMDS hop limit not set to 2, or cluster SG missing rule for port 443 from mgmt SG. | Check `aws-auth` or Access Entries in EKS. Verify the mgmt node's IMDS hop limit. |
| DB replica not bootstrapping | No base backup exists yet, Pod Identity not configured for `orders-db-<colour>`, or wrong S3 bucket path. | Wait for the `ScheduledBackup` on the primary, or trigger one manually. Check IRSA/Pod Identity. |
| Lease stuck on one cluster | Check `cm lease status`. Check `lease-agent` pod logs in both clusters. Verify DynamoDB table has the correct item. | Verify agent RBAC (can it patch CronJobs and KEDA annotations?). |
| Shadow replay "not enough samples" | ALB access logs are delivered every 5 minutes. | Wait 5-10 minutes and retry. Check the S3 bucket to ensure logs are arriving. |
| `cm register` fails | `mgmt` kubeconfig lacks access to the k3s cluster, `argocd` namespace missing, or EKS endpoint unreachable from mgmt. | Verify the SSH tunnel or run `cm` directly on the mgmt node. |
| Pods stuck in Pending after `cluster-up` | Node group not ready, instance type unavailable in the AZ, or Pod Identity webhook not running. | Check EKS console for node group status. |
| Terraform destroy hangs | Usually a dangling ENI or security group dependency. | Check the AWS console for resources still in-use and delete them manually. |
| Traffic shift stuck at 0% green | Verify `TargetGroupBinding` exists and targets are healthy. | Check ALB listener rule ARNs match `config.json`. |
| Write pause longer than expected | CloudNativePG promotion took too long. | Consider adding streaming replication via NLB for faster switchover if object-store recovery is too slow. |

## 4. Monitoring During Migration

While `cm migrate` runs, monitor the following:
- Watch the **Argo Workflow UI** for step-by-step status.
- Monitor **ALB CloudWatch metrics**: `RequestCount`, `HTTPCode_Target_5XX_Count`, `TargetResponseTime`.
- Check `cm report` output after each phase.
- Tail `lease-agent` logs in both clusters during the handoff phase (`kubectl logs -n shop -l app=lease-agent -f`).
- After migration, run `cm verify` to see the reconciliation report.

## 5. Security Considerations

- **Least Privilege:** All IAM roles follow least-privilege (the engine role only has specific permissions defined in `mgmt.tf`).
- **Private EKS API:** The EKS API endpoint is private. It is accessed from the mgmt node via the VPC.
- **Secrets Management:** Database credentials are stored in Secrets Manager and injected via Pod Identity / External Secrets (or fetched directly by the app).
- **OIDC Federation:** GitHub Actions uses OIDC federation (no long-lived credentials stored in GitHub).
- **Vulnerability Scanning:** ECR images should be scanned for vulnerabilities.
- **Management Cluster Access:** The k3s management cluster is not exposed to the internet (accessible only via SSH to the mgmt node).
