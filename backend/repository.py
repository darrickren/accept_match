"""DynamoDB repository: one table (PK/SK strings), the same code for DynamoDB Local and AWS DynamoDB.

Key layout (access pattern -> keys):
  inventory list / item     PK=INV                       SK=ITEM#<item_id>
  admin review queue        PK=REVIEWS                   SK=REVIEW#<review_id>
  demo order / lines        PK=ORDER#<id>                SK=META | LINE#<n> | RETURNLINE#<n> (one return per line)
  return request            PK=RETURN#<id>               SK=META
  session / replies / photo PK=SESSION#<id>              SK=META | REPLY#<client_request_id> | PHOTO#<photo_id>
  defect acceptance         PK=ACCEPT#<user>#<item>#v<n> SK=DEFECT#<defect_id>
  purchase (idempotent)     PK=PURCHASE#<user>#<key>     SK=META
  idempotent responses      PK=IDEMP#<user>#<scope>      SK=KEY#<key>
  counters / seed / audit   PK=COUNTER | SEED | AUDIT#<entity>

Inventory sits in one partition so listings are a strongly consistent Query,
not a table Scan (the table also holds sessions, photos and replies). That is
fine at demo scale; a GSI is the upgrade path. Every record carries
`dataset_id` (demo-v1 for seed data, runtime for records the API creates) so
reset can target demo data only.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from .config import Settings

Key = Tuple[str, str]
_SER, _DES = TypeSerializer(), TypeDeserializer()


class DatabaseUnavailable(RuntimeError):
    pass


class ConditionFailed(RuntimeError):
    pass


class TransactionCancelled(RuntimeError):
    def __init__(self, reasons: List[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_dynamo(value: Any) -> Any:
    """Nested None is kept (stored as NULL) so stored responses replay exactly; top-level None means absent."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


def serialize(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _SER.serialize(_to_dynamo(v)) for k, v in item.items() if v is not None}


def deserialize(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _from_dynamo(_DES.deserialize(v)) for k, v in raw.items()}


# ----------------------------------------------------------------- keys

def inv_key(item_id: str) -> Key:
    return "INV", f"ITEM#{item_id}"


def review_key(review_id: str) -> Key:
    return "REVIEWS", f"REVIEW#{review_id}"


def order_key(order_id: str) -> Key:
    return f"ORDER#{order_id}", "META"


def line_key(order_id: str, line: str) -> Key:
    return f"ORDER#{order_id}", f"LINE#{line}"


def return_line_key(order_id: str, line: str) -> Key:
    return f"ORDER#{order_id}", f"RETURNLINE#{line}"


def return_key(return_id: str) -> Key:
    return f"RETURN#{return_id}", "META"


def session_key(session_id: str) -> Key:
    return f"SESSION#{session_id}", "META"


def reply_key(session_id: str, client_request_id: str) -> Key:
    return f"SESSION#{session_id}", f"REPLY#{client_request_id}"


def photo_key(session_id: str, photo_id: str) -> Key:
    return f"SESSION#{session_id}", f"PHOTO#{photo_id}"


def accept_key(user: str, item_id: str, version: int, defect_id: str) -> Key:
    return f"ACCEPT#{user}#{item_id}#v{version}", f"DEFECT#{defect_id}"


def purchase_key(user: str, idempotency_key: str) -> Key:
    return f"PURCHASE#{user}#{idempotency_key}", "META"


def idem_key(user: str, scope: str, key: str) -> Key:
    return f"IDEMP#{user}#{scope}", f"KEY#{key}"


def counter_key(name: str) -> Key:
    return "COUNTER", name


def seed_key(dataset: str) -> Key:
    return "SEED", dataset


def with_key(key: Key, item: Dict[str, Any]) -> Dict[str, Any]:
    return {"PK": key[0], "SK": key[1], **item}


# ----------------------------------------------------------------- repository

class Repository:
    def __init__(self, client: Any, table: str) -> None:
        self.client = client
        self.table = table

    @classmethod
    def from_settings(cls, settings: Settings) -> "Repository":
        config = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 3, "mode": "standard"})
        if settings.local:
            # Dummy credentials on purpose: local modes can never reach a real AWS table.
            client = boto3.client("dynamodb", endpoint_url=settings.ddb_endpoint, region_name=settings.aws_region,
                                  aws_access_key_id="local", aws_secret_access_key="local", config=config)
        else:  # pragma: no cover - cloud adapters are a later milestone
            client = boto3.client("dynamodb", region_name=settings.aws_region, config=config)
        return cls(client, settings.table)

    # ------------------------------------------------------------ plumbing

    def _call(self, name: str, **kwargs):
        try:
            return getattr(self.client, name)(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise ConditionFailed(name) from None
            if code == "TransactionCanceledException":
                reasons = [r.get("Code", "None") for r in exc.response.get("CancellationReasons", [])]
                raise TransactionCancelled(reasons) from None
            if code in ("ResourceNotFoundException",):
                raise DatabaseUnavailable(f"table {self.table} not found") from None
            raise
        except (EndpointConnectionError, BotoCoreError) as exc:
            raise DatabaseUnavailable(type(exc).__name__) from None

    def ensure_table(self) -> bool:
        """Create the table if missing (local development). Returns True when it was created."""
        try:
            self.client.describe_table(TableName=self.table)
            return False
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
        except (EndpointConnectionError, BotoCoreError) as exc:
            raise DatabaseUnavailable(type(exc).__name__) from None
        self._call("create_table", TableName=self.table, BillingMode="PAY_PER_REQUEST",
                   AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                         {"AttributeName": "SK", "AttributeType": "S"}],
                   KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}])
        self.client.get_waiter("table_exists").wait(TableName=self.table, WaiterConfig={"Delay": 1, "MaxAttempts": 30})
        return True

    def ping(self) -> bool:
        try:
            self._call("describe_table", TableName=self.table)
            return True
        except (DatabaseUnavailable, ClientError):
            return False

    def drop_table(self) -> None:
        self._call("delete_table", TableName=self.table)

    # ------------------------------------------------------------ single items

    def get(self, key: Key, consistent: bool = True) -> Optional[Dict[str, Any]]:
        out = self._call("get_item", TableName=self.table, Key=serialize({"PK": key[0], "SK": key[1]}),
                         ConsistentRead=consistent)
        return deserialize(out["Item"]) if "Item" in out else None

    def put(self, item: Dict[str, Any], if_absent: bool = False, condition: Optional[str] = None,
            values: Optional[Dict[str, Any]] = None, names: Optional[Dict[str, str]] = None) -> None:
        kwargs: Dict[str, Any] = {"TableName": self.table, "Item": serialize(item)}
        if if_absent:
            condition = "attribute_not_exists(PK)" if not condition else f"attribute_not_exists(PK) AND ({condition})"
        if condition:
            kwargs["ConditionExpression"] = condition
        if values:
            kwargs["ExpressionAttributeValues"] = serialize(values)
        if names:
            kwargs["ExpressionAttributeNames"] = names
        self._call("put_item", **kwargs)

    def update(self, key: Key, fields: Dict[str, Any], condition: Optional[str] = None,
               values: Optional[Dict[str, Any]] = None, remove: Iterable[str] = ()) -> Dict[str, Any]:
        """SET each field (and REMOVE `remove`); returns the updated item. Condition values use :c_* names."""
        names, vals, sets = {}, dict(values or {}), []
        remove = list(remove) + [name for name, value in fields.items() if value is None]
        for i, (name, value) in enumerate((n, v) for n, v in fields.items() if v is not None):
            names[f"#f{i}"] = name
            vals[f":v{i}"] = value
            sets.append(f"#f{i} = :v{i}")
        removes = []
        for j, name in enumerate(remove):
            names[f"#r{j}"] = name
            removes.append(f"#r{j}")
        expression = ("SET " + ", ".join(sets)) if sets else ""
        if removes:
            expression += (" " if expression else "") + "REMOVE " + ", ".join(removes)
        kwargs: Dict[str, Any] = {"TableName": self.table, "Key": serialize({"PK": key[0], "SK": key[1]}),
                                  "UpdateExpression": expression, "ExpressionAttributeNames": names,
                                  "ReturnValues": "ALL_NEW"}
        if vals:
            kwargs["ExpressionAttributeValues"] = serialize(vals)
        if condition:
            kwargs["ConditionExpression"] = condition
            for token in [t for t in condition.replace("(", " ").replace(")", " ").split() if t.startswith("#")]:
                names.setdefault(token, token[1:])
        return deserialize(self._call("update_item", **kwargs)["Attributes"])

    def delete(self, key: Key) -> None:
        self._call("delete_item", TableName=self.table, Key=serialize({"PK": key[0], "SK": key[1]}))

    def query(self, pk: str, sk_prefix: Optional[str] = None, consistent: bool = True,
              limit: int = 1000) -> List[Dict[str, Any]]:
        expression, values = "PK = :pk", {":pk": pk}
        if sk_prefix:
            expression += " AND begins_with(SK, :sk)"
            values[":sk"] = sk_prefix
        rows: List[Dict[str, Any]] = []
        start: Optional[Dict[str, Any]] = None
        while True:
            kwargs: Dict[str, Any] = {"TableName": self.table, "KeyConditionExpression": expression,
                                      "ExpressionAttributeValues": serialize(values), "ConsistentRead": consistent}
            if start:
                kwargs["ExclusiveStartKey"] = start
            out = self._call("query", **kwargs)
            rows += [deserialize(r) for r in out.get("Items", [])]
            start = out.get("LastEvaluatedKey")
            if not start or len(rows) >= limit:
                return rows[:limit]

    def scan_keys(self, filter_dataset: Optional[str] = None) -> List[Key]:
        """All keys (optionally for one dataset). Only used by the demo reset script."""
        keys: List[Key] = []
        start = None
        while True:
            kwargs: Dict[str, Any] = {"TableName": self.table, "ProjectionExpression": "PK, SK, dataset_id"}
            if start:
                kwargs["ExclusiveStartKey"] = start
            out = self._call("scan", **kwargs)
            for raw in out.get("Items", []):
                row = deserialize(raw)
                if filter_dataset is None or row.get("dataset_id") == filter_dataset:
                    keys.append((row["PK"], row["SK"]))
            start = out.get("LastEvaluatedKey")
            if not start:
                return keys

    # ------------------------------------------------------------ transactions and counters

    def transact(self, operations: List[Dict[str, Any]]) -> None:
        """operations: dicts with one of put/update/check, already shaped for TransactWriteItems minus TableName."""
        items = []
        for op in operations:
            kind, spec = next(iter(op.items()))
            spec = dict(spec)
            spec["TableName"] = self.table
            if "Item" in spec:
                spec["Item"] = serialize(spec["Item"])
            if "Key" in spec:
                spec["Key"] = serialize(spec["Key"])
            if "ExpressionAttributeValues" in spec:
                spec["ExpressionAttributeValues"] = serialize(spec["ExpressionAttributeValues"])
            items.append({kind: spec})
        self._call("transact_write_items", TransactItems=items)

    def next_id(self, name: str, start: int = 1) -> int:
        """Atomic counter. The first value is `start`."""
        out = self._call("update_item", TableName=self.table, Key=serialize({"PK": "COUNTER", "SK": name}),
                         UpdateExpression="SET #v = if_not_exists(#v, :base) + :one, dataset_id = :ds",
                         ExpressionAttributeNames={"#v": "value"},
                         ExpressionAttributeValues=serialize({":base": start - 1, ":one": 1, ":ds": "runtime"}),
                         ReturnValues="UPDATED_NEW")
        return int(deserialize(out["Attributes"])["value"])

    def audit(self, entity: str, event: str, detail: Dict[str, Any], actor: str) -> None:
        stamp = now_iso()
        seq = self.next_id("audit")
        self.put({"PK": f"AUDIT#{entity}", "SK": f"{stamp}#{seq:06d}", "entity_type": "audit", "event": event,
                  "actor": actor, "detail": detail, "at": stamp, "dataset_id": "runtime"})

    def audit_trail(self, entity: str) -> List[Dict[str, Any]]:
        return self.query(f"AUDIT#{entity}")
