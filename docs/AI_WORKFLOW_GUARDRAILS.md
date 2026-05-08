# AI Workflow Guardrails

This document defines permanent operating rules for AI-assisted work in this repository. Review it before implementation, audits, refactors, migrations, or production fixes.

## Core Rule

Move fast, but move surgically. Prefer the smallest safe change that solves the measured problem. Avoid broad rewrites, speculative refactors, or architecture changes without a clear reason.

## Before Changing Code

- Identify the specific problem and files likely involved.
- Name the expected impact and rollback path.
- Check whether the change affects public traffic, background jobs, billing, auth, customer data, data integrity, or production operations.
- Avoid unrelated files and cleanup.

## AI Behavior Rules

Do not optimize only for "it works." Always consider scalability, maintainability, operational safety, fault isolation, observability, rollback safety, and blast radius.

Do not touch unrelated files, refactor during bug fixes, rewrite working systems without a measured reason, introduce dependencies without justification, or change public contracts casually.

## Architecture Defaults

Prefer queue-based async processing, append-only events or buffers, current-state projections, indexed lookup tables, batching, idempotent jobs, jittered scheduling, and clear workload isolation between public traffic and background jobs.

Avoid giant scans, expensive joins on hot paths, one cron job per customer/entity/monitor, unbounded concurrency, hot-row lock contention, and public pages querying raw historical tables.

## Load and Failure Thinking

For systems that schedule jobs, process events, send HTTP requests, ingest data, update high-write tables, or run recurring work, evaluate behavior under 10x load, 100x load, retries, duplicate execution, partial outages, queue backlog, burst traffic, and slow external dependencies.

## Preferred System Shape

Prefer:

```text
scheduler -> queue -> workers -> reducers/projections
```

Over:

```text
request/cron -> direct synchronous processing -> large database writes/queries
```

Prefer:

```text
raw events -> append-only buffer -> rollups/projections -> public reads
```

Over:

```text
public page -> live aggregate query over raw data
```

## Change Review Checklist

Before finalizing a change, answer what problem it solved, which files changed, the blast radius, what could break, how to roll back, what validation proves it, and whether unrelated behavior was preserved.
