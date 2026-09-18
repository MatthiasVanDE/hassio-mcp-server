# Security Policy

## The threat model, stated plainly

This add-on is an administrative interface to Home Assistant. It runs with
`hassio_role: manager` and a Supervisor token, which means a caller holding a valid
bearer token can read every entity, call every service, rewrite automations, read the
host log, manage add-ons and download backups.

That authority is the point of the add-on. It also means:

- **The bearer token is the entire security boundary.** Use a long random value.
- **The transport is plain HTTP.** Port 8099 must not be reachable from the internet.
  Use a VPN, or a reverse proxy that terminates TLS.
- **`/health` is unauthenticated by design**, because the container's health check
  cannot send a token. It exposes only liveness, the version and the number of tools.

Reports that amount to "an attacker who already has the token can do administrative
things" describe the documented design rather than a vulnerability.

## Supported versions

Only the latest released version is supported.

## Reporting a vulnerability

Please **do not open a public issue.**

Use GitHub's private reporting at
<https://github.com/MatthiasVanDE/hassio-mcp-server/security/advisories/new>, or email
matthias.vanderelst@gmail.com.

Please include what an attacker needs in order to reach the flaw, what they gain, and
a way to reproduce it. You can expect an acknowledgement within a week. This is a spare
time project maintained by one person, so please be realistic about timelines — but a
real issue will be fixed and credited, and I will tell you when it is public.
