#!/usr/bin/env python3
import argparse
import json
import re
import sys
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
import yaml

def _sanitize_pin_name(name: str) -> str:
    # Keep letters, digits, underscore, dash, dot, colon; replace others with dash.
    return re.sub(r"[^A-Za-z0-9_\-.:]", "-", name).strip("-")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QLever Cache CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared args helper
    def add_common(p: argparse.ArgumentParser, with_config: bool) -> None:
        p.add_argument(
            "-u",
            "--url",
            required=True,
            help="QLever endpoint base URL (e.g., http://localhost:7001).",
        )
        if with_config:
            p.add_argument(
                "-c",
                "--config",
                required=True,
                help="Path to YAML with prefixes, properties, patterns, queries.",
            )
        p.add_argument(
            "-t",
            "--token",
            default=None,
            help="Optional access token (sent as Authorization: Bearer <token>).",
        )

    add_common(sub.add_parser("clear-and-pin", help="Clear cache completely, pin warmup, clear unpinned"), with_config=True)
    add_common(sub.add_parser("clear", help="Clear cache completely (including pinned)"), with_config=False)
    add_common(sub.add_parser("clear-unpinned", help="Clear only unpinned cache entries"), with_config=False)
    add_common(sub.add_parser("pin", help="Pin warmup queries generated from YAML"), with_config=True)
    add_common(sub.add_parser("clear-named", help="Clear all named cached results"), with_config=False)
    stats_parser = sub.add_parser("stats", help="Show cache stats")
    add_common(stats_parser, with_config=False)
    stats_parser.add_argument(
        "--detailed",
        action="store_true",
        default=False,
        help="Show detailed statistics and settings",
    )

    return parser.parse_args(argv)


def load_yaml_config(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # Normalize keys
    prefixes = cfg.get("prefixes") or {}
    if not isinstance(prefixes, dict):
        raise ValueError("`prefixes` must be a mapping of prefix -> IRI.")
    properties = cfg.get("properties") or []
    if isinstance(properties, str):
        properties = [p for p in properties.split() if p]
    if not isinstance(properties, list):
        raise ValueError("`properties` must be a list of strings.")
    patterns = cfg.get("patterns") or cfg.get("complex_patterns") or []
    if isinstance(patterns, str):
        patterns = [p for p in patterns.split() if p]
    if not isinstance(patterns, list):
        raise ValueError("`patterns` must be a list of strings.")
    queries_raw = cfg.get("queries") or []
    if isinstance(queries_raw, str):
        queries_raw = [queries_raw]
    if not isinstance(queries_raw, list):
        raise ValueError("`queries` must be a list of strings or objects with `query` (and optional `name`).")
    queries: List[Dict[str, str]] = []
    for item in queries_raw:
        if isinstance(item, str):
            queries.append({"query": item})
        elif isinstance(item, dict):
            if "query" not in item or not isinstance(item["query"], str):
                raise ValueError("Every query object must have a string field `query`.")
            qobj = {"query": item["query"]}
            if "name" in item and item["name"]:
                qobj["name"] = str(item["name"])
            queries.append(qobj)
        else:
            raise ValueError("Each entry in `queries` must be a string or an object with `query` and optional `name`.")
    return {
        "prefixes": prefixes,
        "properties": properties,
        "patterns": patterns,
        "queries": queries,
    }


def build_prefix_string(prefixes: Dict[str, str]) -> str:
    return "\n".join([f"PREFIX {name}: <{iri}>" for name, iri in prefixes.items()])


def extract_used_prefix_names(query: str) -> List[str]:
    # Finds all tokens like prefixName:something in the query and returns unique prefix names in order of appearance.
    tokens = re.findall(r"([A-Za-z_][A-Za-z0-9_\-]*):[A-Za-z_][A-Za-z0-9_\-]*", query)
    # Preserve order while removing duplicates
    seen = set()
    result = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def add_prefixes_used_in_query(query: str, available_prefixes: Dict[str, str]) -> str:
    # If the query explicitly contains %PREFIXES%, replace it with full prefix string and return.
    if "%PREFIXES%" in query:
        return query.replace("%PREFIXES%", build_prefix_string(available_prefixes))
    # Otherwise, collect only used prefixes and prepend them.
    used = extract_used_prefix_names(query)
    prefix_defs = []
    for name in used:
        iri = available_prefixes.get(name)
        if iri:
            prefix_defs.append(f"PREFIX {name}: <{iri}>")
    if prefix_defs:
        return "\n".join(prefix_defs) + "\n" + query
    return query


def normalize_query(query: str) -> str:
    # Replace # in IRIs by %23 temporarily to strip comments safely.
    q = re.sub(r"(<[^>]+)#", r"\1%23", query)
    # Remove comments (# ... end-of-line).
    q = re.sub(r"#.*\n", " ", q, flags=re.MULTILINE)
    # Re-replace %23 by # inside IRIs.
    q = re.sub(r"(<[^>]+)%23", r"\1#", q)
    # Collapse whitespace
    q = re.sub(r"\s+", " ", q)
    # Remove dot before }
    q = re.sub(r"\s*\.\s*}", " }", q)
    return q.strip()


def generate_property_queries(properties: Iterable[str]) -> List[Tuple[str, str]]:
    queries: List[Tuple[str, str]] = []
    for predicate in properties:
        p = predicate.strip()
        if not p or p.startswith("#"):
            continue
        # subject order
        q1 = (
            "SELECT ?subject ?object WHERE { "
            f"?subject {p} ?object "
            "} INTERNAL SORT BY ?subject"
        )
        queries.append((f"{p} ordered by subject", q1))
        # object order
        q2 = (
            "SELECT ?subject ?object WHERE { "
            f"?subject {p} ?object "
            "} INTERNAL SORT BY ?object"
        )
        queries.append((f"{p} ordered by object", q2))
    return queries


def generate_pattern_queries(patterns: Iterable[str]) -> List[Tuple[str, str]]:
    queries: List[Tuple[str, str]] = []
    for pattern in patterns:
        pat = pattern.strip()
        if not pat or pat.startswith("#"):
            continue
        q = (
            "SELECT ?subject ?object WHERE { "
            f"?subject {pat} ?object "
            "} INTERNAL SORT BY ?subject"
        )
        queries.append((f"{pat} ordered by subject only", q))
    return queries


def build_all_queries(cfg: Dict) -> List[Tuple[str, str, Optional[str]]]:
    prefixes = cfg["prefixes"]
    explicit_queries: List[Dict[str, str]] = cfg["queries"]
    queries: List[Tuple[str, str, Optional[str]]] = []
    # Properties
    for label, q in generate_property_queries(cfg["properties"]):
        queries.append((label, q, None))
    # Patterns
    for label, q in generate_pattern_queries(cfg["patterns"]):
        queries.append((label, q, None))
    # Explicit queries
    for i, qobj in enumerate(explicit_queries, start=1):
        q = qobj["query"]
        pin_name = _sanitize_pin_name(qobj["name"]) if "name" in qobj else None
        label = qobj.get("name", f"explicit query {i}")
        queries.append((label, q, pin_name))
    # Add prefixes and normalize
    result: List[Tuple[str, str, Optional[str]]] = []
    for label, q, pin_name in queries:
        q_with_prefixes = add_prefixes_used_in_query(q, prefixes)
        q_norm = normalize_query(q_with_prefixes)
        result.append((label, q_norm, pin_name))
    return result


def execute_query(
    base_url: str,
    query: str,
    token: Optional[str],
    pin_name: Optional[str] = None,
) -> Tuple[Optional[requests.Response], Optional[str]]:
    headers = {"Accept": "application/qlever-results+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = {"query": query, "send": "10"}
    # Use new QLever parameter names: pin-result or pin-result-with-name
    if pin_name:
        data["pin-result-with-name"] = pin_name
    else:
        data["pin-result"] = "true"
    try:
        resp = requests.post(base_url, data=data, headers=headers, timeout=6000.0)
        return resp, None
    except requests.exceptions.RequestException as e:
        return None, str(e)


def request_cmd(base_url: str, cmd: str, token: Optional[str]) -> Tuple[Optional[requests.Response], Optional[str]]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = {"cmd": cmd}
    try:
        resp = requests.post(base_url, data=data, headers=headers, timeout=30.0)
        return resp, None
    except requests.exceptions.RequestException as e:
        return None, str(e)


def run_clear(base_url: str, token: Optional[str], complete: bool) -> int:
    cmd = "clear-cache-complete" if complete else "clear-cache"
    label = "Clear cache completely" if complete else "Clear unpinned cache"
    print(label)
    resp, err = request_cmd(base_url, cmd, token)
    if err or resp is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"ERROR: HTTP {resp.status_code}: {e}", file=sys.stderr)
        print(resp.text[:2000], file=sys.stderr)
        return 1
    print("OK")
    return 0


def run_stats(base_url: str, token: Optional[str], detailed: bool) -> int:
    print("Cache stats")
    # First try with the modern 'cache-stats', then fall back to 'cachestats'.
    resp, err = request_cmd(base_url, "cache-stats", token)
    if err or resp is None or resp.status_code >= 400:
        resp, err = request_cmd(base_url, "cachestats", token)
        if err or resp is None:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"ERROR: HTTP {resp.status_code}: {e}", file=sys.stderr)
        print(resp.text[:2000], file=sys.stderr)
        return 1
    try:
        cache_stats = resp.json()
    except json.JSONDecodeError:
        print(resp.text, file=sys.stderr)
        return 1
    # Get settings (for cache-max-size).
    settings_resp, err = request_cmd(base_url, "get-settings", token)
    if err or settings_resp is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    try:
        settings_resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"ERROR: HTTP {settings_resp.status_code}: {e}", file=sys.stderr)
        print(settings_resp.text[:2000], file=sys.stderr)
        return 1
    try:
        settings = settings_resp.json()
    except json.JSONDecodeError:
        print(settings_resp.text, file=sys.stderr)
        return 1
    if isinstance(settings, list) and settings:
        settings = settings[0]

    # Always print a brief human-friendly summary (MB/GB).
    def _parse_cache_max_size_bytes(val) -> Optional[float]:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            s = val.strip()
            # Formats: "30 GB", "30GB", "30000 MB", "30000000000"
            m = re.match(r"^([\d.]+)\s*(GB|MB)$", s, flags=re.IGNORECASE)
            if m:
                num = float(m.group(1))
                unit = m.group(2).upper()
                return num * (1e9 if unit == "GB" else 1e6)
            # Plain number string -> bytes
            if re.match(r"^\d+(\.\d+)?$", s):
                return float(s)
        return None

    pinned_size_bytes = cache_stats.get("cache-size-pinned", 0)
    unpinned_size_bytes = cache_stats.get("cache-size-unpinned", 0)
    try:
        pinned_bytes = float(pinned_size_bytes)
        unpinned_bytes = float(unpinned_size_bytes)
    except Exception:
        print("ERROR: Invalid numeric values in cache stats", file=sys.stderr)
        return 1
    total_bytes = pinned_bytes + unpinned_bytes
    max_bytes = _parse_cache_max_size_bytes(
        settings.get("cache-max-size") if isinstance(settings, dict) else None
    )
    # Choose display unit.
    display_base = max_bytes if (isinstance(max_bytes, (int, float)) and max_bytes > 0) else total_bytes
    use_gb = display_base >= 1e9
    bytes_factor = 1e9 if use_gb else 1e6
    unit = "GB" if use_gb else "MB"
    pinned = pinned_bytes / bytes_factor
    unpinned = unpinned_bytes / bytes_factor
    if max_bytes and max_bytes > 0:
        cache_size = max_bytes / bytes_factor
        cached = pinned + unpinned
        free = cache_size - cached
        if cached <= 0:
            print(f"Cache is empty, all {cache_size:.1f} {unit} available")
        else:
            print(
                f"Pinned queries     : {pinned:5.1f} {unit} of {cache_size:5.1f} {unit}  "
                f"[{(pinned / cache_size):5.1%}]"
            )
            print(
                f"Non-pinned queries : {unpinned:5.1f} {unit} of {cache_size:5.1f} {unit}  "
                f"[{(unpinned / cache_size):5.1%}]"
            )
            print(
                f"FREE               : {free:5.1f} {unit} of {cache_size:5.1f} {unit}  "
                f"[{(1 - (cached / cache_size)):5.1%}]"
            )
    else:
        # No cache-max-size available, show absolute sizes.
        print(f"Pinned queries     : {pinned:5.1f} {unit}")
        print(f"Non-pinned queries : {unpinned:5.1f} {unit}")

    if not detailed:
        return 0

    # Detailed version: show both dicts as key-value tables.
    def show_dict_as_table(items):
        items = list(items)
        if not items:
            return
        max_key_len = max(len(str(k)) for k, _ in items)
        for key, value in items:
            if isinstance(value, (int, float)):
                v = value
            else:
                v = str(value)
                if isinstance(value, str) and re.match(r"^\d+$", value):
                    v = "{:,}".format(int(value))
                elif isinstance(value, str) and re.match(r"^\d+\.\d+$", value):
                    v = "{:.2f}".format(float(value))
            print(f"{str(key).ljust(max_key_len)} : {v}")

    show_dict_as_table(cache_stats.items())
    print("")
    if isinstance(settings, dict):
        show_dict_as_table(settings.items())
    else:
        print(json.dumps(settings, indent=2))
    return 0


def run_pin(base_url: str, config_path: str, token: Optional[str]) -> int:
    try:
        cfg = load_yaml_config(config_path)
    except Exception as e:
        print(f"Failed to load config: {e}", file=sys.stderr)
        return 2
    all_queries = build_all_queries(cfg)
    successes = 0
    failures = 0
    for idx, (label, query, pin_name) in enumerate(all_queries, start=1):
        print("")
        if pin_name:
            print(f"[{idx}/{len(all_queries)}] {label}  (name: {pin_name})")
        else:
            print(f"[{idx}/{len(all_queries)}] {label}")
        print(query)
        start = time.time()
        resp, err = execute_query(base_url=base_url, query=query, token=token, pin_name=pin_name)
        dt = time.time() - start
        if err:
            print(f"ERROR: request failed: {err} (took {dt:.2f}s)", file=sys.stderr)
            failures += 1
            continue
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"ERROR: HTTP {resp.status_code}: {e} (took {dt:.2f}s)", file=sys.stderr)
            print(resp.text[:2000], file=sys.stderr)
            failures += 1
            continue
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            print(f"ERROR: Non-JSON response (status {resp.status_code}) (took {dt:.2f}s)", file=sys.stderr)
            print(resp.text[:2000], file=sys.stderr)
            failures += 1
            continue
        if "exception" in payload:
            print(f"ERROR from QLever: {payload.get('exception')} (took {dt:.2f}s)", file=sys.stderr)
            failures += 1
            continue
        result_size = payload.get("resultsize")
        print(f"OK: resultsize={result_size} (took {dt:.2f}s)")
        successes += 1
    print("")
    print(f"Pin done. Successes: {successes}, Failures: {failures}")
    # As in qlever-ui warmup, clear the rest of the cache (only unpinned).
    clear_rc = run_clear(base_url, token, complete=False)
    return 0 if failures == 0 and clear_rc == 0 else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    # Normalize possible underscores to hyphens in command name.
    cmd = args.command.replace("_", "-")
    if cmd == "clear":
        return run_clear(args.url, args.token, complete=True)
    if cmd == "clear-unpinned":
        return run_clear(args.url, args.token, complete=False)
    if cmd == "stats":
        return run_stats(args.url, args.token, detailed=args.detailed)
    if cmd == "pin":
        return run_pin(args.url, args.config, args.token)
    if cmd == "clear-and-pin":
        rc = run_clear(args.url, args.token, complete=True)
        if rc != 0:
            return rc
        rc = run_pin(args.url, args.config, args.token)
        if rc != 0:
            return rc
        return run_clear(args.url, args.token, complete=False)
    if cmd == "clear-named":
        resp, err = request_cmd(args.url, "clear-named-cache", args.token)
        if err or resp is None:
            print(f"ERROR: {err}", file=sys.stderr)
            return 1
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"ERROR: HTTP {resp.status_code}: {e}", file=sys.stderr)
            print(resp.text[:2000], file=sys.stderr)
            return 1
        print("Cleared named cached results.")
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())


