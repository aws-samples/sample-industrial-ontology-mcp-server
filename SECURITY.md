# Security Policy

## Reporting a vulnerability

Do not report a potential security issue through a public GitHub issue.
Notify AWS Security at `aws-security@amazon.com` or use the
[AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/).
Include enough detail to reproduce and assess the issue, but do not include
credentials, customer data, or other sensitive information.

## Supported use

Ontology Agent is sample code for a local stdio MCP server. It is intended for
non-production use with synthetic or explicitly approved data. This repository
does not publish versioned security-support commitments or a production
service-level agreement.

The supported sample keeps file operations within documented project
directories and derives the public tool surface from the checked-in manifest.
Treat model-generated RDF, SPARQL, and Cypher as untrusted input and validate
them before use. Keep `.env`, `.mcp.json`, credentials, and generated artifacts
out of Git, use least-privilege IAM and database identities, and require
explicit confirmation before enabling remote writes.

Network exposure and production operation are outside this sample's supported
scope. A production design requires separate authentication, authorization,
transport security, request limiting, audit logging, data isolation, secret
management, dependency maintenance, monitoring, incident response, and
security review.

See the repository [threat model](docs/security/threat-model.md) for the
documented trust boundaries, threats, controls, and verification commands.
