---
name: portainer
description: Running and operating Portainer (CE/BE) for Docker and Swarm: install behind Traefik, agent for remote nodes, stacks from Git with webhooks, access control, secrets and hardening. Use when setting up Portainer, deploying stacks through it, or automating deploys to it from CI.
---

# Portainer

## Install (single host, behind Traefik)
```yaml
services:
  portainer:
    image: portainer/portainer-ce:2.21.5      # pin; upgrade deliberately
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - portainer_data:/data
    networks: [proxy]
    labels:
      - traefik.enable=true
      - traefik.http.routers.portainer.rule=Host(`portainer.example.com`)
      - traefik.http.routers.portainer.entrypoints=websecure
      - traefik.http.routers.portainer.tls.certresolver=le
      - traefik.http.services.portainer.loadbalancer.server.port=9000
volumes: { portainer_data: {} }
networks: { proxy: { external: true } }
```
- Create the admin user within 5 minutes of the first start or the instance locks itself (restart the container to reopen).
- **Swarm / many nodes:** deploy `portainer/agent` as a global service on an overlay network and add the environment pointing at `tasks.agent:9001`; do not expose 9001 publicly.

## Stacks from Git (the deploy path for CI)
1. Stacks > Add stack > Repository (use a read-only deploy key or token), compose path, env vars in the UI (not in the repo).
2. Enable **webhook**: Portainer shows a URL; `curl -fsS -X POST "$PORTAINER_WEBHOOK_URL"` redeploys (pulls the new image tag if the file references it). Treat the URL as a secret.
3. Or the API: `POST /api/stacks/{id}/git/redeploy?endpointId=N` with header `X-API-Key: <access token>` (create a token per pipeline, least-privilege user).
- A webhook redeploy re-pulls only when the tag changes or "Re-pull image" is enabled: use immutable tags (`:sha-abc123`) from CI, not `latest`.

## Hardening
- Portainer with the Docker socket is root on the host: put it behind HTTPS, enable 2FA or OAuth/LDAP, remove the default 9000/9443 public ports, and restrict by IP or VPN.
- Use Teams/RBAC and per-environment access; give developers "Standard user", not admin.
- Back up the `portainer_data` volume (users, stacks, endpoints) and keep the compose file in Git.

## Common problems
- *Stack keeps old image:* tag unchanged; use a new tag or enable re-pull.
- *Env vars missing after redeploy:* they live in the stack's Portainer settings, not in the repo file.
- *Agent shows "down":* overlay network/DNS (`tasks.agent`), time drift between nodes, or agent/server version mismatch.
