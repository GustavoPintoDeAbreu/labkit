---
name: deploy-to-pi
description: Deploy a lab project as a Docker compose service on the Raspberry Pi 5 (arm64, always on). Use when a project should run on the Pi or its Pi deploy needs changing.
---

# Deploy a project to the Pi

The lab's pattern (flight-deals, redditcast) is `deploy/pi/{Dockerfile,docker-compose.yml}`,
built on the Pi itself, with live data in `deploy/pi/data/` and secrets in `deploy/pi/.env`
that exist only on the Pi. Ship it with labkit, not a copied script:

```bash
labkit deploy-pi --name myapp --include 'myapp/***' --include config.yaml \
  [--waha-env-var MYAPP_WAHA_URL] [--init-env] [--dry-run]
```

- It refuses a dirty tree unless `--force` and stamps the commit into `deploy/pi/VERSION`
  (have the Dockerfile `COPY deploy/pi/VERSION VERSION`).
- Images must support `linux/arm64`; the Pi has no GPU (call the PC's broker over the LAN)
  and boots from a slow USB SSD: batch writes, rotate logs (`max-size: 10m`).
- Use your own compose project; join `pi_default` as an external network to reach
  `kaya-waha`. Never put the service inside Kaya's project (it deploys with `--remove-orphans`).
- `restart: unless-stopped`. Never `docker stop` live containers before a shutdown.
- Afterwards: add a labwatch collector or health rule and the repo's `lab.yaml` `runs`
  entry (PRs), so the dashboard and the `lab` MCP know it exists.
