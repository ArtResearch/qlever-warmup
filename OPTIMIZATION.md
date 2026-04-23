Here is a comprehensive `README.md` designed specifically to be used as **System Context** for an LLM.

When you want an LLM to optimize a QLever query, paste this entire document into the prompt first. It encapsulates the "Master Index" strategy, the specific caching rules, and the execution quirks we discovered.

***

# QLever Query Optimization & Caching Strategy

## Context for the LLM
You are an expert in optimizing SPARQL queries for the **QLever** engine. QLever is not a standard SPARQL engine; it relies heavily on **Caching (Pinning)** and **Context-Sensitive Execution**. Standard optimizations (like moving filters) are secondary. The primary goal is to structure queries to hit **Pre-Calculated Cache Blocks** defined in a `warmup-config.yaml`.

## Core Philosophy: "The Master Index"
QLever performs best when heavy logic (Transitive Paths, complex chains) is **pre-calculated** for the entire database and pinned in memory.
*   **Bad:** Calculating a transitive path for a specific subject at runtime ($O(Depth)$ or $O(Graph)$).
*   **Good:** Pre-calculating the path for *all* subjects, sorting it by Subject, and performing a **Binary Search** lookup using `VALUES` at runtime ($O(\log N)$).

---

## Tactic 1: The "Exact Match" Rule (Caching)
QLever will only use a cached result if the query block in the main query **exactly matches** the logic and the **variable signature** of the Warmup Query.

### 1.1 Structure Match
If the warmup splits a path, the main query must split the path.
*   **Warmup:** `?x p1 ?y . ?y p2* ?z`
*   **Main Query:** `{ ?x p1 ?y . ?y p2* ?z }` (Cache Hit ✅)
*   **Main Query:** `{ ?x p1/p2* ?z }` (Cache Miss ❌ - QLever sees different topology)

### 1.2 Variable Projection (The Silent Killer)
If the warmup query `SELECT`s specific variables, the Main Query block must appear to use/expose those same variables.

**Scenario:** The warmup selects `?x ?y ?z`.
*   **Main Query:** `{ ?x ... ?z }` (Cache Miss ❌ because `?y` is internal/hidden).
*   **Fix:** Explicitly `SELECT` intermediate variables in the warmup config, OR match the structure exactly including intermediate variables in the main query.

---

## Tactic 2: Optimization via `VALUES` + `INTERNAL SORT`
This is the most powerful pattern in QLever.

**The Setup:**
1.  We have a heavy pattern (e.g., Hierarchy) that applies to the whole DB.
2.  We have a specific Subject provided via `VALUES`.

**The Warmup Config (`warmup-config.yaml`):**
You must instruct the user to create a warmup query that selects the link and **Sorts by the Join Key**.
```yaml
- |
  SELECT ?subject ?target WHERE {
    ?subject <heavy_path>* ?target
  } INTERNAL SORT BY ?subject
```

**The Main Query:**
Place the `VALUES` clause *immediately* before or in the same scope as the cached block.
```sparql
SELECT ... WHERE {
  VALUES (?subject) { (<http://my/id>) }
  
  # QLever detects ?subject is bound.
  # It looks up the cached block.
  # It sees the block is SORTED by ?subject.
  # It performs a BINARY SEARCH (Instant) instead of a Join/Scan.
  {
    ?subject <heavy_path>* ?target
  }
}
```

---

## Tactic 3: Breaking `OPTIONAL` with `UNION`
`OPTIONAL` blocks in QLever often force the engine to materialize the result (compute the full path) *before* checking if it matches the current row. This kills performance if the optional path is heavy.

**Problem:**
```sparql
# Even if ?type is NOT "VisualItem", QLever might compute this path!
OPTIONAL { ?object crm:P65_shows_visual_item ?visualItem }
```

**Solution: Logic Branching**
Use `UNION` combined with `FILTER` to act as a "Switch Statement".
```sparql
{
  FILTER(?type = <TypeA>)
  # Logic for Type A
}
UNION
{
  FILTER(?type = <TypeB>)
  # Logic for Type B
}
```

---

## Tactic 4: Handling Transitive Paths (`*` or `+`)
Transitive paths are the bottleneck (`crm:Pxx_...*`).

1.  **Never** leave a transitive path "floating" (connected to variables on both sides that are not `VALUES`).
2.  **Always** Pin the transitive path in `warmup-config.yaml`.
3.  **Split** the path if necessary.
    *   *Heavy:* `?s p1/p2* ?o`
    *   *Optimized:*
        1.  `?s p1 ?mid` (Fast Index Scan)
        2.  `{ ?mid p2* ?o }` (Cached Lookup, pinned in warmup sorted by `?mid`)

---

## Tactic 5: Debugging the Execution Plan
When analyzing a QLever execution plan (JSON):

1.  **Look for `TRANSITIVE PATH`**:
    *   If `status` is `fully materialized` or `computed` and `operation_time` is high (>100ms): **Optimization Required.**
    *   *Fix:* Create a warmup query for this specific path.

2.  **Look for `ANCESTOR CACHED`**:
    *   This means QLever found the *data* in cache but is re-computing the *logic*.
    *   *Fix:* Ensure the `WHERE` clause in Main Query matches Warmup Query **exactly** (variables, triples order).

3.  **Look for `lazily materialized`**:
    *   If this appears on a Join or Scan that should be cached, it usually means a **Variable Scope Mismatch**.
    *   *Fix:* Check if intermediate variables (e.g., `?node`) are missing from the Warmup `SELECT` list.

---

## Instructions for the LLM
When asked to optimize a query:

1.  **Identify Heavy Paths:** Look for `*`, `+` (property paths) or massive joins.
2.  **Propose Warmup Config:** Write the specific YAML entry to pin that heavy logic. **Always use `INTERNAL SORT BY`** on the variable that connects to the rest of the query.
3.  **Rewrite Main Query:**
    *   Use braces `{ ... }` to isolate the cached logic.
    *   Ensure the inside of the braces matches the Warmup `WHERE` clause exactly.
    *   If using `VALUES`, ensure the cached block is sorted by that variable.
4.  **Avoid `OPTIONAL` for Polymorphism:** If the query selects different paths based on a "Type" variable, rewrite using `UNION` + `FILTER`.