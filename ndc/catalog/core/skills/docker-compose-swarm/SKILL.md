---
name: docker-compose-swarm
description: Production Docker Compose and Docker Swarm stacks: healthchecks, resource limits, restart and update policies, secrets, configs, networks, volumes, rolling updates and rollback, backups. Use when writing or reviewing docker-compose.yml / stack files for servers, or deploying with `docker stack deploy`.
---

# Compose and Swarm in production

## Per-service checklist
- **Image:** pinned tag (never `latest`), ideally by digest for critical services. Build in CI, pull on the server.
- **Healthcheck:** every long-running service; deploy ordering uses it (`depends_on: {db: {condition: service_healthy}}` in Compose).
- **Limits:** `deploy.resources.limits/reservations` (Swarm) or `mem_limit`/`cpus` (Compose). A service without a memory limit can take the node down.
- **Restart:** Compose `restart: unless-stopped`; Swarm `deploy.restart_policy.condition: on-failure`.
- **Logs:** `logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}` or disks fill up.
- **User:** run as non-root (`user:`), `read_only: true` + `tmpfs` where possible, `cap_drop: [ALL]`.
- **Secrets:** Swarm `secrets:` (mounted at `/run/secrets/<name>`, use the `*_FILE` env convention) instead of plain env vars. Compose: `.env` outside git, never baked into images.
- **Data:** named volumes for state; bind mounts only for config. Databases never on the ephemeral layer.

## Swarm stack essentials
```yaml
services:
  api:
    image: ghcr.io/me/api:1.4.2
    networks: [proxy, backend]
    healthcheck: { test: ["CMD", "wget", "-qO-", "http://localhost:3000/health"], interval: 15s, timeout: 3s, retries: 3, start_period: 20s }
    deploy:
      replicas: 2
      update_config: { order: start-first, parallelism: 1, failure_action: rollback, monitor: 30s }
      rollback_config: { order: start-first }
      restart_policy: { condition: on-failure, delay: 5s, max_attempts: 3 }
      resources: { limits: { cpus: "1", memory: 512M }, reservations: { memory: 128M } }
      placement: { constraints: [node.role == worker] }
      labels: [traefik.enable=true]        # Swarm: labels go under deploy
networks:
  proxy: { external: true }
  backend: { driver: overlay, internal: true }   # no outbound/inbound from outside
```
- Deploy: `docker stack deploy -c stack.yml --with-registry-auth --prune app`. Rollback: `docker service rollback app_api`.
- `start-first` needs capacity for old + new replicas; stateful services (DB) use `stop-first` and one replica pinned by a placement constraint.
- Swarm ignores `build:`, `depends_on`, `container_name`; `docker compose config` does not validate stack-only keys, so check with `docker stack config -c stack.yml`.
- Overlay traffic between nodes uses 2377/tcp, 7946/tcp+udp, 4789/udp: open them only between the nodes. Encrypt with `--opt encrypted` on sensitive networks.
- Drain a node before maintenance: `docker node update --availability drain <node>`.

## Operations
- `docker system df` then `docker image prune -a --filter until=168h` on a schedule; never `volume prune` blindly.
- Backups: `docker run --rm -v vol:/data -v $PWD:/backup alpine tar czf /backup/vol.tgz -C /data .` for volumes; logical dumps for databases (`pg_dump`, `mysqldump`). Test a restore.
- Debug order: `docker service ps --no-trunc <svc>` (rejection reason), `docker service logs`, `docker inspect`, then network (`docker network inspect`).
