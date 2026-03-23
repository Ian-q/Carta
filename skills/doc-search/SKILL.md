# /doc-search Skill

Search the Carta knowledge graph with a natural language query and present results with source citations.

---

## Step 1: Accept the query

The user invokes this skill with a natural language query, e.g.:

> `/doc-search decoupling requirements for power rails`

Extract the query string. If the user invoked the skill with no query text, ask:

> "What would you like to search for?"

---

## Step 2: Run the Carta search command

Run:

```bash
python .carta/carta/cli.py search "<query>"
```

Replace `<query>` with the user's query string, properly quoted.

Wait for the command to complete. Capture stdout and stderr.

---

## Step 3: Parse and present results

Parse the command output. Results are returned as a ranked list with at minimum: document path, score, and a text excerpt.

Present results in this format:

```
## Search Results for "<query>"

### 1. <document title or filename>
**Path:** `<path/to/doc>`
**Score:** <score>
**Excerpt:** <relevant excerpt>

### 2. ...
```

Show the top 5 results. If the result set is empty, say "No results found for that query."

---

## Step 4: Fallback — grep if Qdrant unreachable

If the `carta search` command exits with an error indicating Qdrant is unreachable (look for "connection refused", "collection not found", or "unreachable" in stderr):

1. Notify the user: "Qdrant is unreachable — falling back to text search."
2. Run a grep-based fallback. Split the query into keywords and search:

```bash
grep -r --include="*.md" --include="*.txt" --include="*.yaml" -l "<keyword>" .
```

Run once per significant keyword (skip stop words). Combine the file lists (union). For each matched file, show the file path and the matching line with context (`grep -n -C 2`).

Present fallback results with a note that these are keyword matches, not semantic matches.

---

## Step 5: Offer follow-up

After presenting results, offer:

> "Would you like to open any of these documents, run `/doc-audit` to check for issues, or refine the search?"
