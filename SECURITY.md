# Security policy

## Supported versions

Security fixes are released for the latest minor version only.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through GitHub:
**Security → Report a vulnerability** on this repository (private vulnerability reporting).

Include the affected version (`GET /healthz` reports it), how to reproduce the issue and its impact.
You will get a first response within a week.

## Deployment notes

CPI Log Lens has no built-in login and is meant to run on your own machine; by default it only
listens on `127.0.0.1`. Do not expose it to a network without an authenticating reverse proxy.
Tenant secrets are stored in the database file; protect the data volume and its backups.
