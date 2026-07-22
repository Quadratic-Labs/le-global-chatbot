# systemd configuration

This directory contains the operating-system configuration required to run the
L&E Global Legal Chatbot Docker Compose stack automatically.

## Files

- `le-global-chatbot.service` — starts and stops the complete Docker Compose stack.
- `99-le-global-chatbot.conf` — configures the Linux kernel settings required by
  OpenSearch and Redis.

## Managed services

The systemd unit manages the following containers:

- FastAPI backend
- OpenSearch
- OpenSearch Dashboards
- Redis

All services are currently managed through a single Docker Compose project located at:

```text
/opt/le-global-chatbot/infra
