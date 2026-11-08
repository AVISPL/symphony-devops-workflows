# Dependency inventory

Builds a third-party (open-source) dependency inventory across the Symphony
microservices and publishes it as an Allure report.

## What it does

`deps_inventory.py` reads `list-micro.json`, and for each repo:

1. Resolves a git ref (prefers `release/6.27`, else latest `release/x.y`, else default branch).
2. Fetches `pom.xml` (and module poms) from the GitHub org via the REST API.
3. Extracts **explicit** `<dependencies>`, dropping `com.avispl.*` (third-party only).
4. For deps without an inline version, records the **managing source** (symphony-shared-parent /
   spring-boot-dependencies / google libraries-bom …) instead of resolving the version.
5. Enriches each distinct artifact with **license + origin** from Maven Central.
6. *(optional)* Cross-checks each library against the approved **3rd-Party Software** Confluence
   page (match by name/URL; version ignored). No match → flagged for review.

Outputs: `out/dependencies-by-repository.md`, `out/dependencies.csv`,
`out/unique-dependencies.md`, and Allure results in `allure-results/`
(one test per repository × dependency; not-on-approved-list = failed test, suite = repository).

## Workflow

`.github/workflows/dependency_inventory.yml` — manual (`workflow_dispatch`). Publishes the Allure
report to GitHub Pages under `dependency-inventory/` and uploads the raw md/csv as a run artifact.

## Required secrets

| Secret | Required | Purpose |
|---|---|---|
| `ciToken` | yes | GitHub PAT with **read** access to the AVISPL org repos (reads each `pom.xml`). |
| `confluenceBaseUrl` | optional | e.g. `https://avi-spl.atlassian.net` — enables the approved-list check. |
| `confluenceEmail` | optional | Atlassian account email for the API token. |
| `confluenceApiToken` | optional | Atlassian API token (Basic auth with the email). |

If any `confluence*` secret is missing, the approved-list check is skipped and the inventory is
still produced.

## Run locally

```bash
export GH_TOKEN="$(gh auth token)"
# optional: export CONFLUENCE_BASE_URL=... CONFLUENCE_EMAIL=... CONFLUENCE_API_TOKEN=...
python3 deps_inventory.py --list list-micro.json --org AVISPL \
  --out-dir out --allure-results allure-results \
  --prefer-ref release/6.27 --confluence-page 803569665
```

## Notes / limitations

- License is read from each artifact's latest Maven Central release (license type is stable across
  versions; exact in-use version is intentionally not resolved).
- The approved-list match is a name/URL heuristic — `RED` means "no automatic match", which can be a
  false positive when a library is listed under a different product name. Review before acting.
- The Confluence reader targets the v2 REST API (`/wiki/api/v2/pages/{id}?body-format=storage`);
  verify on first run against your page.
