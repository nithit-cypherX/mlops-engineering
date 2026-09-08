# Week 0 — setup evidence

This directory records preparation, not an additional graded Lab 0.

Completed before workspace migration:
- Ubuntu 22.04 under WSL2; Python 3.11.16, Make 4.3, Docker hello-world passed.
- Azure CLI 2.90.0; Azure for Students authenticated.
- Resource group: `itcs355-u6688124`.
- Storage account and Basic container registry: `itcs355u6688124`.
- Private Blob container: `itcs355`; scoped Blob Data Contributor permission.
- Docker registry login passed.
- Course resource-group budget: USD 24, alerts at 50/90/100 percent. Ends 2026-10-20 00:00 UTC, covering the final class on October 19. Alerts do not automatically stop services.
- [Original cloud check](cloud-check.txt): 11/11 passed.

Region exception: the subscription policy blocks southeastasia. Storage and ACR use malaysiawest, which the policy permits; the resource group's metadata location remains southeastasia.

Work now runs from `../lab1/`. Its `cloud.env` and `.venv/` are local and ignored by Git. This does not mean Lab 1 is complete.

Pending before class: submit the setup evidence through the instructor's chosen process; read the technical-debt paper and prepare one question. Check screenshots for private information before adding them to `evidence/`.
