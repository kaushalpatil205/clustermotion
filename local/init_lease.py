"""Create the local lease table and give the lease to LEASE_HOLDER."""
import os
import time

import boto3

TABLE = os.environ["LEASE_TABLE"]
HOLDER = os.environ.get("LEASE_HOLDER", "local")
ddb = boto3.client("dynamodb", region_name="us-east-1")

for _ in range(60):
    try:
        ddb.list_tables()
        break
    except Exception:
        time.sleep(1)

try:
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "lease_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "lease_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
except ddb.exceptions.ResourceInUseException:
    pass

ddb.put_item(
    TableName=TABLE,
    Item={
        "lease_id": {"S": "singletons"},
        "holder": {"S": HOLDER},
        "desired": {"S": HOLDER},
        "renewed_at": {"N": str(int(time.time()))},
    },
)
print(f"lease 'singletons' held by {HOLDER}")
