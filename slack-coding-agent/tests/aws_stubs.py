"""Minimal boto3/botocore stand-ins so lambda modules can be imported in tests.

The unit-test venv intentionally has no boto3. These stubs only satisfy the
import-time ``import boto3`` / ``import botocore.exceptions`` statements; the
lambdas under test get their clients monkeypatched per test (fake tables etc).
"""
import sys
import types


def _make_boto3():
    boto3 = types.ModuleType("boto3")

    class _TableMaker:
        def __init__(self):
            self.tables = {}

        def Table(self, name):
            if name not in self.tables:
                raise LookupError(f"no fake table registered for {name!r}")
            return self.tables[name]

    table_maker = _TableMaker()

    def resource(service_name, *args, **kwargs):
        if service_name != "dynamodb":
            raise LookupError("fake boto3 only supports dynamodb resource")
        return table_maker

    boto3.resource = resource
    boto3.client = lambda *a, **k: None
    boto3._table_maker = table_maker
    return boto3


def _make_botocore():
    botocore = types.ModuleType("botocore")

    class ClientError(Exception):
        def __init__(self, response, operation_name=None):
            super().__init__(response)
            self.response = response
            self.operation_name = operation_name

    class DynamoDBClientError(ClientError):
        pass

    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    botocore.exceptions = exceptions
    sys.modules["botocore.exceptions"] = exceptions
    return botocore


def install_stubs():
    if "boto3" not in sys.modules:
        sys.modules["boto3"] = _make_boto3()
    if "botocore" not in sys.modules:
        sys.modules["botocore"] = _make_botocore()
        # re-export for compatibility with attribute-only imports
    return sys.modules["boto3"]


def fake_dynamodb_resource():
    return sys.modules["boto3"]._table_maker


def client_error(code, message="conditional request failed"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}})