---
name: traefik
description: Traefik v3 as reverse proxy for Docker Compose and Swarm: routers, services, middlewares via labels, Let's Encrypt (HTTP and DNS challenge), TLS, security headers and dashboard protection. Use when exposing containers behind Traefik or debugging routing, certificates or 404/502 errors.
---

# Traefik (v3, Docker provider)

## Static config (traefik.yml or flags)
```yaml
entryPoints:
  web:
    address: ":80"
    http: { redirections: { entryPoint: { to: websecure, scheme: https } } }
  websecure: { address: ":443" }
providers:
  docker:
    exposedByDefault: false          # opt-in per container: traefik.enable=true
    network: proxy                   # the shared network Traefik reaches containers on
    # Swarm: use `swarm:` instead of `docker:` and put labels under deploy.labels
certificatesResolvers:
  le:
    acme:
      email: you@example.com
      storage: /letsencrypt/acme.json   # chmod 600, persist in a volume
      httpChallenge: { entryPoint: web }
```

## Compose
```yaml
services:
  traefik:
    image: traefik:v3.1              # pin the minor version
    command: ["--configFile=/traefik.yml"]
    ports: ["80:80", "443:443"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./traefik.yml:/traefik.yml:ro
      - letsencrypt:/letsencrypt
    networks: [proxy]
  app:
    image: ghcr.io/me/app:1.4.2
    networks: [proxy]
    labels:
      - traefik.enable=true
      - traefik.http.routers.app.rule=Host(`app.example.com`)
      - traefik.http.routers.app.entrypoints=websecure
      - traefik.http.routers.app.tls.certresolver=le
      - traefik.http.services.app.loadbalancer.server.port=3000   # required if the image EXPOSEs 0 or 2+ ports
networks:
  proxy: { name: proxy }
volumes: { letsencrypt: {} }
```

## Rules that prevent most incidents
- **Socket = root on the host.** Mount `docker.sock` read-only and preferably through a socket proxy (`tecnativa/docker-socket-proxy`, allow only `CONTAINERS=1`).
- **Dashboard:** never `--api.insecure=true` on a public host. Route it on its own host with `basicAuth`/`forwardAuth` middleware, or bind it to localhost.
- **Middlewares** are attached by label: `...routers.app.middlewares=sec@docker`, defined with `traefik.http.middlewares.sec.headers.stsSeconds=31536000`, `...headers.contentTypeNosniff=true`, `...headers.frameDeny=true`. Shared ones go in a file provider.
- **Let's Encrypt rate limits:** test with the staging `caServer` first. Wildcards need the DNS challenge (provider token in env or Docker secret, never in labels).
- **Behind Cloudflare proxy:** use the DNS challenge (HTTP challenge fails) and set `forwardedHeaders.trustedIPs` to Cloudflare's ranges.
- **Sticky sessions / health:** `loadbalancer.healthcheck.path=/health`, `loadbalancer.sticky.cookie=true` only when the app needs it.

## Debugging
1. `docker logs traefik` (set `log.level: DEBUG` temporarily). Look for `Router defined multiple times` and `cannot be linked to a service`.
2. **404:** wrong `Host()` rule, `exposedByDefault` true/false mismatch, or router on another entrypoint.
3. **502/504:** Traefik and the app share no network (`providers.docker.network`), wrong `loadbalancer.server.port`, or app listens on 127.0.0.1 instead of 0.0.0.0.
4. **Cert not issued:** `acme.json` permissions, port 80 unreachable, DNS not pointing at the host yet, rate limit.
5. Labels with `$` in Compose need `$$` (bcrypt hashes for basicAuth).
