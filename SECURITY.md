# Security policy

## Reporting a vulnerability

**Do not open a public issue** for security flaws. Use one of these two private channels:

1. The **Security → Report a vulnerability** tab of this repository (GitHub private security advisory); or
2. email security@ariadne-project.com with the subject "Ariadne: security".

Please tell us what you found, how to reproduce it, and the affected version (or commit). Include only what is needed to
demonstrate the issue, and never real credentials or other people's data.

What to expect:

- **Acknowledgement within 5 working days.** Ariadne is maintained by a small team, so please be patient.
- **An assessment within 14 days**: whether we confirm the issue, its severity, and what we plan to do.
- **A fix or mitigation** as soon as we can: usually within 90 days, faster for critical issues. If it will take longer,
  we will tell you why.
- **Credit** in the release notes if you want it.

We ask that you give us time to fix the issue before disclosing it publicly, and we will coordinate the disclosure date
with you.

## Scope

In scope: the code in this repository (`server`, `web`, the Dockerfiles and the scripts in `deploy/`) and the standalone
compose. In particular: key-based authentication, the secrets vault (`ARIADNE_MASTER_KEY`), the license class that keeps
private content out of releases, the code sandbox (Jobe) and the Moodle proxy's SSRF protection.

Out of scope: flaws in the projects Ariadne uses (Kiwix, Wiki.js, Moodle, Calibre, Kavita, Jobe, LibreTranslate,
Postgres…); report those to their maintainers. A misconfigured installation (ports open to the internet, a shared `.env`)
does not count either.

## Supported versions

Only the latest published release receives security fixes.

## If you publish an installation

- Do not expose `postgres`, `jobe` or `calibre`: the compose keeps them on its internal network, so do not add `ports:` for them. Only the `server`, Kiwix, Wiki.js, Moodle and Kavita ports are published; restrict them to the networks that need them.
- Keep `.env` and `ARIADNE_MASTER_KEY` out of git; change the generated passwords only if you know what you are doing.
- Put the `server` behind TLS (a reverse proxy) and use a separate API key for each agent.
