# Security policy

## Supported versions

Only the latest release of the National Grid US integration receives security fixes. Please update to the newest version through HACS before reporting a problem.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately with GitHub's **Report a vulnerability** button on the [Security tab](https://github.com/virtitnerd/ha_nationalgrid/security/advisories/new) of this repository. This keeps the details private until a fix is available.

Please include:

- what the problem is and what an attacker could do with it
- the integration version and Home Assistant version
- steps to reproduce, or a short description of how you found it

Please do not include your National Grid password, tokens, or account numbers in a report. Redact them first.

This project is maintained by one person, so replies are on a best-effort basis. Confirmed vulnerabilities are fixed in a patch release as quickly as practical, and you will be credited in the advisory if you would like to be.

## Scope

In scope: the code in this repository, including how the integration stores credentials, logs data, and handles diagnostics.

Out of scope: National Grid's own website and services, Home Assistant core, and HACS. Please report those to their owners.

## Dependencies

Dependabot security updates are enabled. The integration relies on the [py-nationalgrid](https://github.com/virtitnerd/py-nationalgrid) library for talking to National Grid, which has its own security policy.
