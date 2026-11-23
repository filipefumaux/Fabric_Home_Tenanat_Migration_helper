## Helpers
Calls the Fabric, OneLake, ADLS, Eventhouse and Azure DevOps APIs with the signed-in user's token,
waiting on throttling (HTTP 429) and retrying transient errors.
