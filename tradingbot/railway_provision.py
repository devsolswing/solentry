"""Railway per-user bot provisioning — GATED, OFF by default.

This module is never wired into signup and never runs in tests. It only
runs when an admin explicitly calls provision_bot_service() AND both
conditions hold:
  * env RAILWAY_PROVISIONING == "1"
  * a Railway account/project token is supplied (env RAILWAY_TOKEN)

It talks to Railway's public GraphQL API (endpoint and variable shapes
match the custom.railway connector doc): serviceCreate to make a service
inside a project, variableCollectionUpsert to set that service's variables.

It logs/returns only service and variable NAMES — never values. A
provisioning run that would carry a bot's trading key passes the key as a
variable VALUE to Railway only; the value is never printed, logged, or
stored by this module.
"""

from __future__ import annotations

import json
import urllib.request

RAILWAY_GRAPHQL_URL = "https://backboard.railway.com/graphql/v2"

SERVICE_CREATE_MUTATION = """
mutation ServiceCreate($input: ServiceCreateInput!) {
  serviceCreate(input: $input) { id name }
}
"""

VARIABLE_COLLECTION_UPSERT_MUTATION = """
mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) {
  variableCollectionUpsert(input: $input)
}
"""


class ProvisioningDisabled(Exception):
    pass


def is_enabled(config) -> bool:
    """True only when the flag AND a token are both present."""
    return bool(config.railway_provisioning and config.railway_token)


def _graphql(token: str, query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        RAILWAY_GRAPHQL_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "User-Agent": "trading-bot-app"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "errors" in body:
        raise RuntimeError(
            "Railway API error: "
            + "; ".join(str(e.get("message", e)) for e in body["errors"]))
    return body.get("data", {})


def provision_bot_service(config, project_id: str, environment_id: str,
                          service_name: str, variables: dict,
                          http_post=None) -> dict:
    """Create a Railway service for one user's bot and set its variables.

    Returns {"service_id": ..., "service_name": ..., "variables_set": [names]}.
    Raises ProvisioningDisabled unless flag + token are both set.
    `variables` values are sent to Railway and never logged or returned.
    `http_post` is a test seam: (query, variables) -> data dict.
    """
    if not is_enabled(config):
        raise ProvisioningDisabled(
            "Railway provisioning is OFF: set RAILWAY_PROVISIONING=1 and "
            "RAILWAY_TOKEN to enable it.")
    token = config.railway_token

    def call(query: str, variables_in: dict) -> dict:
        if http_post is not None:
            return http_post(query, variables_in)
        return _graphql(token, query, variables_in)

    created = call(SERVICE_CREATE_MUTATION, {
        "input": {"projectId": project_id, "name": service_name}})
    service = (created or {}).get("serviceCreate") or {}
    service_id = service.get("id")
    if not service_id:
        raise RuntimeError("Railway serviceCreate returned no service id")

    call(VARIABLE_COLLECTION_UPSERT_MUTATION, {
        "input": {"projectId": project_id,
                  "environmentId": environment_id,
                  "serviceId": service_id,
                  "variables": variables}})
    # Names only — values never leave this function in a return/log.
    return {"service_id": service_id, "service_name": service_name,
            "variables_set": sorted(variables.keys())}
