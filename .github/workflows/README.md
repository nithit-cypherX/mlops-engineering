# CI workflows

`lab4-ci.yml` tests each change, pushes the tested image on main, then deploys that digest to lab4-staging and runs three smoke checks. Staging access requires the configured environment and OIDC trust. Live deployment has not been verified yet.

Instructor examples are in materials/upstream/.github/workflows/.
