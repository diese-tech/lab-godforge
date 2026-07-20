# GodForge Web: Current Gaps

This file contains only active web work. Product sequencing is canonical in
[`../docs/STANDALONE_PRODUCT_PLAN.md`](../docs/STANDALONE_PRODUCT_PLAN.md);
implementation is tracked by its linked GitHub issues.

## Guild authorization

- Enforce Discord guild permissions on every settings mutation.
- Show only guilds the signed-in user can manage.
- Verify GodForge is installed before enabling setup controls.
- Replace role labels with managed Discord role IDs.

## Durable per-guild configuration

- Store settings, custom commands, dashboard preferences, and asset mappings by
  guild ID.
- Record who changed settings and when.
- Add migration/export tooling before changing storage formats.

## Active standalone tools

- Keep Discord and web formatting separate while sharing selection rules.
- Add end-to-end coverage for command create, edit, disable, and delete.
- Add deeper tests for randomizer, draft, and responsive layouts.
- Reconcile the dashboard party view with the durable party repository after
  zero-config guild setup lands.

## Production readiness

- Complete guild-permission authorization tests.
- Replace temporary password access with production Discord authorization.
- Add favicon, Open Graph image, `robots.txt`, `sitemap.xml`, and canonical URL.
- Use [`ASSET_MANIFEST.md`](ASSET_MANIFEST.md) when licensed production assets
  are available.

Historical prototype and economy-dashboard work belongs in
[`../docs/archive/`](../docs/archive/) and is not part of the current roadmap.
