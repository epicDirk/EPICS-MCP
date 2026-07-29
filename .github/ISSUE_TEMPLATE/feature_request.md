---
name: Feature request
about: Suggest a tool, CLI, or capability for the EPICS MCP server
title: "[feature] "
labels: enhancement
---

## Problem / use case

What are you trying to do with the control system that the server does not support today?

## Proposed capability

What tool, CLI flag, resource, or plane would help, and roughly what it should return.

## Read-only / safety note

This server reads by default and mutates only through a gate: `set_pv_value` is triple-gated, and
the Olog logbook writes sit behind their own separate gate. It reaches nothing until a launcher
configures the EPICS address lists / service URLs. If your request involves writes or reaching a
non-local endpoint, please say so explicitly and describe the safety posture you expect.

## Alternatives considered

Anything you already tried (another tool, a manual query, ChannelFinder/Archiver directly).
