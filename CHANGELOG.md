# Changelog

All notable changes to this project are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); while the version is below 1.0,
a minor bump may add or change CSV columns.

## [Unreleased]

### Fixed

- In-game memberships sold as add-ons with a billing term in the name, such
  as "Fortnite Crew - 1 Month", are now classified as `Subscription` instead
  of `DLC / Add-on`, without needing a manual override. The
  `classification_evidence` names the matched term.

## [0.2.1] - 2026-11-20

### Fixed

- A manual `base_game_key` or `base_game_title` on a subscription was kept in
  the base-game columns but never linked, leaving a half-linked row with no
  warning. It is now ignored explicitly: `enrich` prints how many subscription
  products were affected, and their `relationship_evidence` explains that
  subscriptions never link to a base game and that `game` should be used.

## [0.2.0] - 2026-11-19

### Added

- Enriched CSV relationship columns, placed after `product_key`:
  `add_on_subtype`, `add_on_subtype_source`, `add_on_subtype_evidence`,
  `relationship_type`, `base_game_key`, `base_game_title`,
  `relationship_status`, `relationship_source`, `relationship_evidence`,
  `base_game_ownership`, `base_game_match` and `base_game_order_item_id`.
  Add-ons are linked to the purchase that owns their base game.
- `product_key`: a locale-scoped, normalised Store product key for durable joins.
- `game`: the game a purchase counts towards, for per-game totals. Blank for
  subscriptions, promotions and wallet top-ups.
- `inclusion_reason`: with `--paid-only`, `paid` or `base_game_reference`.
- `enrich --include-base-games` adds the earliest zero-cost purchase of each
  base game that a paid add-on belongs to.
- `curate` command and the private
  `~/.psn-transactions/relationship_overrides.csv` file for manual category,
  base-game and `game` decisions. It lists conflicts, unresolved add-ons,
  possible duplicate games and unclassified products, with candidate base
  games for each.

### Changed

- Enriched CSV column order: the new columns sit between `product_key` and
  `enrichment_status`. Consumers that read columns by position, not by
  header, need updating.
- Successful SKU cache records written before relationship metadata existed
  are refreshed automatically on the next `enrich` run.

## [0.1.0]

Untagged baseline: login, HTTP and browser fetch, date ranges, CSV export and
Store-enriched export.

[0.2.1]: https://github.com/thuds0n/psn-transaction-history/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/thuds0n/psn-transaction-history/compare/0f79397...v0.2.0
