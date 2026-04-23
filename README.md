# QLever Cache CLI

Manage the QLever cache: clear, clear unpinned, view stats, and pin warmup queries generated from a YAML config. The behavior mirrors what qlever-ui uses for cache warmup:

- Generate warmup queries from a YAML file (prefixes, properties, patterns, and explicit queries)
- Execute them against a QLever endpoint with pinresult=true
- Clear cache (complete or unpinned) and show cache statistics


## Install

```bash
pip install -r requirements.txt
```

Requires Python 3.8+.


## Commands

Syntax:

```bash
python qlever_cache_cli.py <command> --url <QLEVER_URL> [--config <file.yaml>] [--token <TOKEN>]
```

Supported commands:

- clear-and-pin: Clear cache completely, pin warmup queries, then clear unpinned
- clear: Clear cache completely (including pinned)
- clear-unpinned: Clear only unpinned cache entries
- pin: Pin warmup queries generated from YAML
- stats: Print cache stats from QLever
- clear-named: Clear all named cached results

Options:

- --url: QLever endpoint base URL (required for all commands), e.g. http://localhost:7001
- --config: Path to YAML for warmup (required for pin and clear_and_pin)
- --token: Optional access token; sent as `Authorization: Bearer <token>`


## YAML Format

The YAML file defines:

- prefixes: Map of prefix label to IRI
- properties: List of properties (CURIEs or IRIs). For each property, two warmup queries are generated:
  - ordered by subject (`INTERNAL SORT BY ?subject`)
  - ordered by object (`INTERNAL SORT BY ?object`)
- patterns: List of complex patterns (SPARQL property paths or composed predicates). For each pattern, one warmup query is generated ordered by subject.
- queries: List of explicit SPARQL queries to pin. Each item can be either:
  - A string with the query
  - An object with:
    - name: Optional cache name; if provided, the query is pinned as a named result (pin-result-with-name)
    - query: The SPARQL query
  If a query contains `%PREFIXES%`, it will be replaced with all prefix declarations from `prefixes`. Otherwise, only the prefixes actually used in the query are prepended automatically.

Example (CIDOC-CRM, prefix `crm`):

```yaml
prefixes:
  crm: "http://www.cidoc-crm.org/cidoc-crm/"

properties:
  # Event -> Actor
  - crm:P14_carried_out_by
  # Production -> Produced Work
  - crm:P108_has_produced
  # Work -> Title
  - crm:P102_has_title

patterns:
  # Event -> Actor -> Appellation (name of the actor)
  - "crm:P14_carried_out_by / crm:P131_is_identified_by"
  # Work -> Produced by Production -> Actor
  - "^crm:P108_has_produced / crm:P14_carried_out_by"

queries:
  # Explicit example: works having titles containing 'portrait'
  - name: "portrait-works"
    query: |
      %PREFIXES%
      SELECT ?work ?title WHERE {
        ?work crm:P102_has_title ?title .
        FILTER(CONTAINS(LCASE(STR(?title)), "portrait"))
      } LIMIT 50
```


## Examples

Pin queries from YAML:

```bash
python qlever_cache_cli.py pin \
  --url http://localhost:7001 \
  --config warmup-cidoc.yaml
```

Clear cache completely and pin, then clear unpinned:

```bash
python qlever_cache_cli.py clear-and-pin \
  --url http://localhost:7001 \
  --config warmup-cidoc.yaml
```

Clear cache completely (including pinned):

```bash
python qlever_cache_cli.py clear --url http://localhost:7001
```

Clear unpinned entries only:

```bash
python qlever_cache_cli.py clear-unpinned --url http://localhost:7001
```

Show cache stats:

```bash
python qlever_cache_cli.py stats --url http://localhost:7001
```

Clear named cached results:

```bash
python qlever_cache_cli.py clear-named --url http://localhost:7001
```


## Implementation Notes

- Pinning uses QLever POST with `query`, `send=10`, and either:
  - `pin-result=true` (unnamed pin), or
  - `pin-result-with-name=<name>` (named pin)
  with `Accept: application/qlever-results+json`.
- After pinning, the CLI clears unpinned cache entries (same as qlever-ui warmup).
- Cache operations and stats use QLever `cmd` endpoints (`clear-cache-complete`, `clear-cache`, `cache-stats`/`cachestats`, `clear-named-cache`) with `Accept: application/json`.
- If `--token` is provided, it is sent as `Authorization: Bearer <token>`.
- For generated queries:
  - Properties: two orders (subject/object)
  - Patterns: ordered by subject
  - Explicit: left as-is, with `%PREFIXES%` substitution if present
  - Only prefixes used in a query are prepended unless `%PREFIXES%` is used


## Exit Codes

- 0: Success
- 1: One or more requests failed
- 2: Invalid or unreadable config


