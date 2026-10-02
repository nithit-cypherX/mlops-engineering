# CI workflows

`lab4-ci.yml` tests each change, pushes the tested image on main, then deploys that digest to lab4-staging and runs three smoke checks. Staging access requires the configured environment and OIDC trust. Staging deployment, all three smoke checks, and client-only access cleanup passed in [CI run 36961138955](https://github.com/nithit-cypherX/mlops-engineering/actions/runs/36961138955).

Instructor examples are in materials/upstream/.github/workflows/.
