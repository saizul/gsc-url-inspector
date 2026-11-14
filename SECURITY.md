# Security policy

## Supported versions

Security fixes are made for the latest release. Please upgrade before reporting.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | yes       |

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting instead: open the **Security** tab of
https://github.com/saizul/gsc-url-inspector and choose **Report a
vulnerability**.

Include what you found, how to reproduce it and what an attacker could do with
it. You should get an answer within a week. Once a fix is released you will be
credited in the changelog unless you prefer otherwise.

**Never include real credentials** (service account keys, OAuth client secrets,
access or refresh tokens) in a report. Redact them, and rotate any key that may
have been exposed.

## Scope

In scope:

- leaking credentials or tokens (token cache permissions, log output, reports)
- the OAuth loopback flow (state handling, PKCE, the listener)
- parsing of untrusted input: sitemaps, URL files and API responses
- HTML report escaping

Out of scope: the Google APIs themselves, and problems that need an attacker who
can already write to your token cache or state database.
