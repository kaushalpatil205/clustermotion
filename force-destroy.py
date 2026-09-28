import json, subprocess, time

REGION = "us-east-1"

def run(cmd):
    try:
        cmd_with_region = f"{cmd} --region {REGION}"
        out = subprocess.check_output(cmd_with_region, shell=True, stderr=subprocess.STDOUT)
        return out.decode("utf-8").strip()
    except subprocess.CalledProcessError as e:
        error_msg = e.output.decode("utf-8").strip()
        if "ResourceNotFoundException" in error_msg or "does not exist" in error_msg or "NotFound" in error_msg:
            return ""
        return f"ERROR: {error_msg}"

def get_json(cmd):
    res = run(cmd)
    if not res or res.startswith("ERROR"): return {}
    try: return json.loads(res)
    except: return {}

print("🔥 Finishing up the cleanup (Route53, SGs, and VPC)...")

print("\n1/3 Deleting Route53 Zone...")
zones = get_json("aws route53 list-hosted-zones-by-name --dns-name clustermotion.internal --output json").get("HostedZones", [])
for z in zones:
    zid = z["Id"]
    recs = get_json(f"aws route53 list-resource-record-sets --hosted-zone-id {zid} --output json").get("ResourceRecordSets", [])
    changes = [{"Action": "DELETE", "ResourceRecordSet": r} for r in recs if r["Type"] not in ("SOA", "NS")]
    if changes:
        # Fixed the JSON structure for Route53 ChangeBatch
        batch = {"ChangeBatch": {"Changes": changes}}
        with open("/tmp/r53.json", "w") as f: f.write(json.dumps(batch))
        run(f"aws route53 change-resource-record-sets --hosted-zone-id {zid} --cli-input-json file:///tmp/r53.json")
    print(f"Deleting Zone {zid}...")
    err = run(f"aws route53 delete-hosted-zone --id {zid}")
    if err.startswith("ERROR"): print(err)

print("\n2/3 Breaking Security Group Dependencies...")
vpcs = get_json("aws ec2 describe-vpcs --filters \"Name=tag:Name,Values=clustermotion\" --output json").get("Vpcs", [])
for vpc in vpcs:
    vid = vpc["VpcId"]
    sgs = get_json(f"aws ec2 describe-security-groups --filters \"Name=vpc-id,Values={vid}\" --output json").get("SecurityGroups", [])
    
    # Step A: Revoke all rules to break cyclic dependencies
    for sg in sgs:
        sg_id = sg['GroupId']
        if sg["GroupName"] == "default": continue
        print(f"Stripping rules from {sg_id}...")
        if sg.get("IpPermissions"):
            run(f"aws ec2 revoke-security-group-ingress --group-id {sg_id} --ip-permissions '{json.dumps(sg['IpPermissions'])}'")
        if sg.get("IpPermissionsEgress"):
            run(f"aws ec2 revoke-security-group-egress --group-id {sg_id} --ip-permissions '{json.dumps(sg['IpPermissionsEgress'])}'")
            
    # Step B: Delete the Security Groups
    for sg in sgs:
        sg_id = sg['GroupId']
        if sg["GroupName"] == "default": continue
        print(f"Deleting Security Group {sg_id}...")
        err = run(f"aws ec2 delete-security-group --group-id {sg_id}")
        if err.startswith("ERROR"): print(err)

print("\n3/3 Deleting VPC...")
for vpc in vpcs:
    vid = vpc["VpcId"]
    print(f"Finally Deleting VPC {vid}...")
    err = run(f"aws ec2 delete-vpc --vpc-id {vid}")
    if err.startswith("ERROR"): print(err)

print("\n✅ Final Cleanup Completed! Check your AWS console, the VPC and Route53 should be gone.")
