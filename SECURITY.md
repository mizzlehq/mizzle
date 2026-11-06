# Security policy

## Supported versions

mizzle is pre-1.0. Security fixes are made on the latest release and `main`.

| Version | Supported |
|---|---|
| 0.2.x (latest) | :white_check_mark: |
| older | :x: |

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use GitHub's private reporting: go to the
[Security tab](https://github.com/mizzlehq/mizzle/security) and click
**Report a vulnerability**.

Include, as far as you can:

- the affected version (`python -c "import mizzle; print(mizzle.__version__)"`),
- what the attacker can do and what they need (network position, a crafted database,
  a hostile schema name, ...),
- a minimal reproduction, and
- any suggested fix.

We aim to acknowledge within 3 business days and to ship a fix or a mitigation plan
within 30 days. We will credit you in the release notes unless you ask to stay anonymous.

## Scope

mizzle talks to a PostgreSQL database and, with the optional Studio extra, serves a local
web UI. Things we consider in scope:

- SQL injection from values, identifiers, schema names or migrated objects.
- Leaking credentials or row data: in logs, error messages, tracebacks or the Studio API.
- Studio: authentication bypass, DNS rebinding, path traversal in the downloaded UI
  archive, unsafe archive extraction, and the `--read-only` guarantee.
- Migrations: bypassing the advisory lock, the tamper check or the data-loss guard.

Out of scope: a compromised machine or database, anyone who already holds
`DATABASE_URL`, denial of service against your own database, and issues that only
reproduce with unsupported or end-of-life dependencies.

## Security model in brief

- Every value is a bind parameter; identifiers are quoted and validated. No API pastes a
  value straight into SQL text.
- Database errors are raised `from None` so logged tracebacks cannot print row values from
  the driver's `DETAIL`; parameter values are not logged unless `log_params=True` is set
  explicitly.
- Studio binds to `127.0.0.1`, rejects non-local `Host` headers, requires a random token on
  every API request (carried in the URL fragment, never sent to the server), sends no CORS
  headers, and throttles wrong credentials per client. The UI archive is downloaded over
  HTTPS and verified against the SHA-256 pinned in the wheel.
- Studio's `--read-only` guarantee rests on the database role: raw SQL runs in a read-only
  transaction, and Studio refuses to start read-only as a superuser or as a role with
  server-side privileges (`pg_read_server_files`, `pg_write_server_files`,
  `pg_execute_server_program`, `pg_signal_backend`, `REPLICATION`). The statement and
  function blocklist it also applies is defence in depth, not the control: a bypass of the
  blocklist alone, with a non-privileged role, is not a read-only break.
