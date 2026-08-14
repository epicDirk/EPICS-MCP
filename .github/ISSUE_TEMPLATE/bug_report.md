---
name: Bug report
about: Report incorrect behaviour in the EPICS MCP server, a tool, or a CLI
title: "[bug] "
labels: bug
---

## What happened

A clear description of the incorrect behaviour.

## What you expected

What you expected instead.

## Reproduction

- Tool / CLI / resource involved (e.g. `get_pv_value`, `epics-crossplane`, `diagnose_connection`):
- Minimal arguments / input (a small `.bob`, a PV name, a config value):
- Steps to reproduce:

## Environment

- `epics-mcp` version (or commit):
- Python version:
- OS:
- Which REST planes are enabled (ChannelFinder / Archiver / Alarm / Naming / Olog, via their
  `*_URL`)?
- Relevant EPICS network config (`EPICS_PVA_ADDR_LIST` / name server), if a live read is involved:

## Logs / output

Paste the error, the tool output, or a relevant `logger` line (redact any hostnames/PVs you
consider sensitive). Please do not paste secrets or `Authorization` header values: the REST planes
each take an `EPICS_MCP_*_AUTH` credential. An error string names the request URL without its
userinfo, but a `logger` line at DEBUG carries it in full, so a pasted log can disclose one.
