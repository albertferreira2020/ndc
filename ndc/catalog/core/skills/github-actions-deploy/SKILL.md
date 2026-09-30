---
name: github-actions-deploy
description: GitHub Actions CI/CD for build, test and deploy: workflow structure, caching, matrix, OIDC and secrets, building/pushing Docker images to GHCR, deploying over SSH, to Portainer webhooks or Swarm, environments with approvals, and hardening. Use when writing or reviewing .github/workflows or debugging a failing pipeline.
---

# GitHub Actions: CI and deploy

## CI baseline
```yaml
name: ci
on:
  pull_request:
  push: { branches: [main] }
concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }
permissions: { contents: read }          # default least privilege; widen per job
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4      # or setup-python
        with: { node-version: 22, cache: npm }
      - run: npm ci && npm run lint && npm test
```

## Build and push image (GHCR) then deploy
```yaml
  image:
    needs: test
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions: { contents: read, packages: write }
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with: { registry: ghcr.io, username: "${{ github.actor }}", password: "${{ secrets.GITHUB_TOKEN }}" }
      - uses: docker/build-push-action@v6
        with:
          push: true
          tags: ghcr.io/${{ github.repository }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
  deploy:
    needs: image
    runs-on: ubuntu-latest
    environment: production              # add required reviewers in repo settings for approval
    steps:
      # (a) SSH to a Compose/Swarm host
      - uses: appleboy/ssh-action@v1.2.0     # pin to a commit SHA in real use
        with:
          host: ${{ secrets.HOST }}
          username: deploy
          key: ${{ secrets.SSH_KEY }}
          script: |
            set -e
            cd /srv/app
            IMAGE_TAG=${{ github.sha }} docker compose pull && IMAGE_TAG=${{ github.sha }} docker compose up -d --remove-orphans
      # (b) or Portainer: curl -fsS -X POST "${{ secrets.PORTAINER_WEBHOOK }}"
```
Swarm variant of (a): `docker service update --image ghcr.io/me/app:${SHA} --with-registry-auth app_api`.

## Rules
- **Pin third-party actions to a full commit SHA** (tags are mutable); let Dependabot update them. First-party `actions/*` by major tag is acceptable.
- **Secrets:** repo/environment secrets only; never echo them; they are not passed to workflows from forks. Prefer **OIDC** (`id-token: write`) to cloud providers over long-lived keys.
- **`pull_request_target` and `workflow_run`** run with secrets on untrusted input: never check out or execute PR code there.
- **Injection:** never put `${{ github.event.* }}` (titles, branch names, bodies) straight into `run:`; pass it via `env:` and quote the variable.
- **Deploy key:** a dedicated `deploy` user with only `docker` rights and a restricted key; `known_hosts` pinned instead of disabling host checks.
- **Immutable tags:** deploy the commit SHA, keep `latest` optional. Rollback = redeploy the previous SHA (`workflow_dispatch` with an input).
- **Speed:** cache dependencies (`cache:` in setup actions), `concurrency` to cancel stale runs, `paths:` filters for monorepos, matrix with `fail-fast: false` when results are independent.
- **Environments:** `production` with required reviewers and a branch rule; keep one workflow for staging (auto) and production (manual approval).

## Debugging
- Re-run with debug logs (`ACTIONS_STEP_DEBUG=true` secret). `act` runs workflows locally but differs on services and secrets.
- *Permission denied pushing to GHCR:* missing `packages: write`, or the package is linked to another repo.
- *Cache never hits:* key changes every run (hash of a generated file), or different branches (caches are scoped to the branch and its base).
- *SSH step hangs:* host key prompt, firewall (allow GitHub runner IPs or use a self-hosted runner/VPN), or key without a newline at the end.
