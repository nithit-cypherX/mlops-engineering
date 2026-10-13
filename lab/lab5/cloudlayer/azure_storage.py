"""Explicit Lab 5 storage targets; no client construction or resource creation."""

from cloudlayer.lab5_scope import TAGS

STORAGE_ACCOUNT = "itcs355u6688124"
INPUTS_DATASTORE = "lab5_inputs"
ARTIFACTS_DATASTORE = "lab5_artifacts"
CONTAINERS = {INPUTS_DATASTORE: "lab5-inputs", ARTIFACTS_DATASTORE: "lab5-artifacts"}


def validate_datastore(datastore, *, name: str) -> None:
    """Check metadata only; never request an account key or SAS.

    The caller must use the reviewed workspace. This does not check Blob RBAC,
    container public-access settings, or prevent a later configuration change.
    """
    from azure.ai.ml.entities import AzureBlobDatastore, NoneCredentialConfiguration

    if name not in CONTAINERS:
        raise ValueError("Only the two reviewed Lab 5 datastores are allowed")
    if not isinstance(datastore, AzureBlobDatastore) or any(
        getattr(datastore, key, None) != expected for key, expected in {
            "name": name, "account_name": STORAGE_ACCOUNT,
            "container_name": CONTAINERS[name], "protocol": "https",
            "endpoint": "core.windows.net",
        }.items()
    ):
        raise ValueError("Datastore does not match the reviewed Lab 5 storage target")
    if not isinstance(datastore.credentials, NoneCredentialConfiguration):
        raise ValueError("Lab 5 datastore must use identity access, not an account key or SAS")
    if not isinstance(datastore.tags, dict) or any(
        datastore.tags.get(key) != value for key, value in TAGS.items()
    ):
        raise ValueError("Datastore is missing Lab 5 ownership tags")
