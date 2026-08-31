# Read-only production evidence — 2026-08-30

Desktop Codex verified SSH using existing alias `sprintbox-itp` (port 2222). The old CLAUDE.md key path does not exist on this PC; do not use port 22 or guess credentials. No secrets have been copied.

- `cf-bot` active; cwd `/opt/content-factory`; `/opt/content-factory/.venv/bin/python -m content_factory.bot.run`.
- Bot API getMe confirmed `Sendpr1ce_bot`, id 8639233666.
- getChat confirmed review id `-5428762212`, group title `Тест_контент_завод`.
- getChat confirmed publish id `-1002268470509`, channel title `СплитХаб.ру (SplitHub.ru)`.
- Shared state `/opt/content-factory/state/content_factory.db`: existing awaiting statuses pending 398, published 197, rejected 9, regen 1. MUST dedupe using original SKU/series keys against this existing state, not just a new hash-scoped importer namespace. These counts include other product categories; do not modify unrelated queue rows.
- Live Avito feed `/opt/oasis/staticfiles/avito-feed.xml`: 175 ads; SHA256 `081f92640ab0b0b8f8036a6b0a62f5ebedc84e8dfbd526a7e6344a7e0995fab6`, matches local audit. None of its image URLs point to `/static/avito-cards/`: they are supplier images. Thus feed alone CANNOT supply the user's generated cards. Match cards through generator/Avito state.
- `/opt/avito-bridge/state/card_jobs.db`: 144 done jobs; columns key, input_filename, status, tries. Legacy keys e.g. `НС-1478151`.
- Generator `/root/ritualb2b/queue.db`, jobs columns include id, input_filename, output_filename, status, specs, brand, model, caption, result_specs. Recent boiler jobs 2185–2187 are done (Royal Thermo Smalto Inverter / XL / Trend); `specs` contains actual source brief, but `caption` and `result_specs` are null. Do NOT assert a ready-made USP exists when it is only technical brief; use exact saved generator brief + factual description fallback.
- Generated media root `/opt/oasis/staticfiles/avito-cards`; generator output `/root/ritualb2b/output`. Match through `card_jobs.input_filename` to `jobs.input_filename` and exact series/SKU. Validate actual image bytes/hash. New Avito card keys use supplier namespace; legacy files must remain supported carefully.
- Water heater generation was a separate run: `/opt/avito-bridge/state/water_oil_runner_20260830.py` uses catalog category IDs `[22,30]` and excludes flow-through/gas-column water heaters. Category 30 boilers are absent from current 175-ad XML, although images have been generated. Do not drop them silently: report separate source case and require final current Bridge price/stock provenance for importing them.

Parent is gathering `state/avito-live-inventory.json` containing product metadata only. Children must not perform network/deployment/secret operations. Public posting remains disallowed until the owner approves each preview. Full task is in AVITO-TELEGRAM-TASK.md.
