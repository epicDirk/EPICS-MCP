---
name: Feature request
about: Suggest a tool, CLI, or capability for the EPICS PV MCP server
title: "[feature] "
labels: enhancement
---

## Problem / use case

What are you trying to do with the control system that the server does not support today?

## Proposed capability

What tool, CLI flag, resource, or plane would help — and roughly what it should return.

## Read-only / safety note

This server is read-only by default (the single write tool is triple-gated) and localhost-isolated.
If your request involves writes or reaching a non-local endpoint, please say so explicitly and
describe the safety posture you expect.

## Alternatives considered

Anything you already tried (another tool, a manual query, ChannelFinder/Archiver directly).
