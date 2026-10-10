"""Temporal holdout: do co-change edges mined before a cut predict what changes after it?

The replay asks "which docs?" and carries its counts forward one commit at a
time. This asks the plainer question, file to file, and freezes everything at
one point: edges are built from the mainline history up to a cut, and then
for every later commit each file it touched takes a turn as the seed. A
method is given the seed alone and must rank the other files of that commit.

Nothing after the cut reaches any method: the pair table is built from the
commits before it and never updated, the popularity baseline counts the same
commits, and the candidate set is the files that existed at the cut. A file
created later can be neither a seed nor an answer, since nothing frozen at
the cut could know it.

A deletion is not a co-change: the miner drops a deleted file and counts
nothing for it, so a path a commit deletes is neither a seed nor an answer
here either. The questions asked are the ones the edges are defined to answer.

History is read as a stream. A commit over the bulk limit is counted and
never collected, so memory follows the pair tables and the tree at the cut,
not the size of the largest commit or the length of the history.

An external file-to-file graph (``--graph NAME=PATH``, JSON or TSV adjacency)
is scored as one more method on the identical queries, so a code graph from
another tool can be compared with history on the same footing. Carta cannot
check when such a graph was built: build it from the tree at the cut, or its
row is not a fair one.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import stat
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from carta.config import ConfigError
from carta.graph.cochange import PairTable, countable_path, mine_rows
from carta.graph.eval.metrics import RESAMPLES, aggregate, bootstrap_ci, paired_bootstrap
from carta.graph.gitio import (
    GitError, first_parent_rows, is_unborn, ls_tree, rev_parse, toplevel,
)
from carta.graph.limits import (
    MAX_COCHANGE_COMMIT_FILES, MAX_EXTERNAL_GRAPH_BYTES, MAX_EXTERNAL_GRAPH_EDGES,
)
from carta.graph.safeio import write_text_replacing
from carta.graph.settings import DEFAULT_SETTINGS, CochangeSettings, GraphSettings

HOLDOUT_SCHEMA = 1
DEFAULT_KS = (1, 3, 5, 10)
BASELINES = ("popularity", "directory")
SKIP_REASONS = ("single_file", "bulk", "unknown_files")
DEFAULT_CUT_SHARE = 0.8
DEFAULT_MIN_POPULATION = 30

Adjacency = Dict[str, Dict[str, float]]


class HoldoutError(ValueError):
    """The holdout cannot run as asked (not a repository, a bad cut, an unreadable graph)."""


# -- external graphs ---------------------------------------------------------------

def _clean_path(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    path = value.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/") or not countable_path(path):
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def _weight(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


def _triples_from_json(data: object):
    if isinstance(data, dict) and isinstance(data.get("edges"), list):
        data = data["edges"]
    if isinstance(data, list):
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) in (2, 3):
                yield item[0], item[1], (item[2] if len(item) == 3 else 1.0)
            elif isinstance(item, dict):
                yield item.get("src"), item.get("dst"), item.get("weight", 1.0)
            else:
                yield None, None, None
        return
    if isinstance(data, dict):
        for src, targets in data.items():
            if isinstance(targets, dict):
                for dst, weight in targets.items():
                    yield src, dst, weight
            elif isinstance(targets, list):
                for dst in targets:
                    yield src, dst, 1.0
            else:
                yield None, None, None
        return
    raise HoldoutError("the graph file must hold a list of edges or a mapping of adjacency")


def _triples_from_text(text: str):
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) == 2:
            yield fields[0], fields[1], 1.0
        elif len(fields) == 3:
            yield fields[0], fields[1], fields[2]
        else:
            yield None, None, None


def load_external_graph(path: Path, *, directed: bool = False) -> Tuple[Adjacency, dict]:
    """Read a file-to-file graph: ``(adjacency, info)``.

    JSON (by its ``.json`` suffix): ``{"edges": [[src, dst], [src, dst, weight], ...]}``,
    a bare list of the same, ``{"src": ["dst", ...]}`` or ``{"src": {"dst": weight}}``.
    Anything else is read as text, one ``src<TAB>dst[<TAB>weight]`` per line,
    ``#`` for comments. Paths are repo-relative with ``/``.

    Unless ``directed``, an edge counts both ways (an import graph is
    directed, but "what changes with this file" is not). A weight defaults to
    1; an edge given twice keeps its larger weight. Rows that are not a valid
    edge are counted in ``info["rejected"]``, never guessed at. The file's
    size and edge count are bounded before it is parsed.
    """
    path = Path(path)
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise HoldoutError(f"{path} is not a regular file")
        if info.st_size > MAX_EXTERNAL_GRAPH_BYTES:
            raise HoldoutError(f"{path} is larger than {MAX_EXTERNAL_GRAPH_BYTES} bytes")
        with open(path, "rb") as handle:
            data = handle.read(MAX_EXTERNAL_GRAPH_BYTES + 1)
    except OSError as exc:
        raise HoldoutError(f"could not read {path}: {exc.strerror or 'I/O error'}") from None
    if len(data) > MAX_EXTERNAL_GRAPH_BYTES:
        raise HoldoutError(f"{path} is larger than {MAX_EXTERNAL_GRAPH_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HoldoutError(f"{path} is not UTF-8 text") from None
    if path.suffix.lower() == ".json":
        try:
            triples = _triples_from_json(json.loads(text))
        except (ValueError, RecursionError):
            raise HoldoutError(f"{path} is not valid JSON") from None
    else:
        triples = _triples_from_text(text)
    graph: Adjacency = {}
    edges = rejected = 0
    for raw_src, raw_dst, raw_weight in triples:
        src, dst, weight = _clean_path(raw_src), _clean_path(raw_dst), _weight(raw_weight)
        if src is None or dst is None or weight is None or src == dst:
            rejected += 1
            continue
        edges += 1
        if edges > MAX_EXTERNAL_GRAPH_EDGES:
            raise HoldoutError(f"{path} holds more than {MAX_EXTERNAL_GRAPH_EDGES} edges")
        for a, b in ((src, dst),) if directed else ((src, dst), (dst, src)):
            links = graph.setdefault(a, {})
            if weight > links.get(b, 0.0):
                links[b] = weight
    return graph, {"edges": edges, "rejected": rejected, "nodes": len(graph),
                   "directed": bool(directed)}


# -- scoring -------------------------------------------------------------------------

def _score(gold_ranks: Sequence[int], n_gold: int, returned: int, ks: Sequence[int]) -> dict:
    """One query's row, from the 1-based ranks at which gold files were returned."""
    recall, precision, any_hit = {}, {}, {}
    for k in ks:
        found = sum(1 for rank in gold_ranks if rank <= k)
        recall[k] = found / n_gold
        precision[k] = found / k
        any_hit[k] = 1.0 if found else 0.0
    first = min(gold_ranks) if gold_ranks else 0
    return {"recall_at": recall, "precision_at": precision, "any_hit_at": any_hit,
            "rr": 1.0 / first if first else 0.0, "returned": float(min(returned, max(ks))),
            "covered": 1.0 if returned else 0.0}


def _mean_rows(rows: Sequence[dict], ks: Sequence[int]) -> dict:
    """The mean of several query rows: one commit's row."""
    n = len(rows)
    out: Dict[str, Any] = {}
    for name in ("recall_at", "precision_at", "any_hit_at"):
        out[name] = {k: math.fsum(row[name][k] for row in rows) / n for k in ks}
    for name in ("rr", "returned", "covered"):
        out[name] = math.fsum(row[name] for row in rows) / n
    return out


def _list_ranks(ranked: Sequence[str], gold: Set[str]) -> List[int]:
    return [i + 1 for i, path in enumerate(ranked) if path in gold]


# -- the run -------------------------------------------------------------------------

def _cut_index(repo: Path, shas: Sequence[str], cut: Optional[str], cut_share: float) -> int:
    """How many commits are training history: the commits ``[0, index)``."""
    if cut is not None:
        try:
            wanted = rev_parse(repo, cut)
        except GitError as exc:
            raise HoldoutError(f"--cut {cut!r} does not name a commit: {exc}") from exc
        if wanted not in shas:
            raise HoldoutError(
                f"--cut {cut!r} is not on the first-parent line being scored; "
                f"the cut must be a mainline commit")
        return list(shas).index(wanted) + 1
    if not (isinstance(cut_share, float) and 0.0 < cut_share < 1.0):
        raise HoldoutError(f"the cut share must be between 0 and 1, got {cut_share!r}")
    return int(len(shas) * cut_share)


def run_holdout(repo: Path, *, rev: str = "HEAD", cut: Optional[str] = None,
                cut_share: float = DEFAULT_CUT_SHARE, ks: Sequence[int] = DEFAULT_KS,
                settings: Optional[GraphSettings] = None,
                variants: Optional[Mapping[str, CochangeSettings]] = None,
                graphs: Optional[Mapping[str, Tuple[Adjacency, dict]]] = None,
                max_files: Optional[int] = None, popularity_window: int = 0,
                min_population: int = DEFAULT_MIN_POPULATION,
                resamples: int = RESAMPLES, per_commit: bool = False,
                config: Optional[str] = None) -> dict:
    """Score co-change edges frozen at a cut against the commits after it.

    ``variants`` maps a method name to the co-change settings it is mined
    with (default: one method, ``cochange``, with ``settings.cochange``), so
    two measures or two decay rates are compared on one population.
    ``graphs`` maps a name to what ``load_external_graph`` returned; each is
    scored as ``graph:<name>``. ``popularity_window`` limits the popularity
    baseline to the last N training commits (0: all of them).

    The unit is the commit: a commit's row is the mean over its seeds, and
    every interval is a bootstrap over commits, so one large commit does not
    outvote many small ones.
    """
    config = "defaults" if settings is None else (config or "caller")
    settings = settings or DEFAULT_SETTINGS
    resamples = int(resamples)
    if resamples < 1:
        raise HoldoutError(f"resamples must be at least 1, got {resamples}")
    ks = tuple(sorted(set(int(k) for k in ks)))
    if not ks or ks[0] < 1:
        raise HoldoutError("k values must be positive integers")
    variants = dict(variants) if variants else {"cochange": settings.cochange}
    graphs = dict(graphs or {})
    for name in list(variants) + list(graphs):
        if not name or any(ch.isspace() or ord(ch) < 0x20 for ch in name):
            raise HoldoutError(f"method name {name!r} must not be empty or hold whitespace")
    external = {f"graph:{name}": value for name, value in sorted(graphs.items())}
    clash = (set(variants) & set(BASELINES)) | (set(variants) & set(external))
    if clash:
        raise HoldoutError(f"method name {sorted(clash)[0]!r} is taken")
    max_files = int(settings.cochange.max_files if max_files is None else max_files)
    if not 2 <= max_files <= MAX_COCHANGE_COMMIT_FILES:
        raise HoldoutError(f"max_files must be between 2 and {MAX_COCHANGE_COMMIT_FILES}, "
                           f"got {max_files}")
    window = int(popularity_window)
    if window < 0:
        raise HoldoutError(f"the popularity window must not be negative, got {window}")

    root = toplevel(Path(repo))
    if root is None:
        raise HoldoutError(f"{repo} is not inside a git repository")
    try:
        head = rev_parse(root, rev)
    except GitError as exc:
        # Only a branch with no commit yet is an empty history. A revision that
        # names nothing is a mistake, and must not pass for a quiet repository.
        if rev != "HEAD" or not is_unborn(root):
            raise HoldoutError(f"--rev {rev!r} does not name a commit: {exc}") from exc
        mainline: List[List[str]] = []
    else:
        mainline = first_parent_rows(root, head)
    shas = [row[0] for row in mainline]
    n_train = _cut_index(root, shas, cut, cut_share) if mainline else 0
    train, test = mainline[:n_train], mainline[n_train:]

    # Everything a method may know, built from the commits before the cut only.
    tables = {name: PairTable(cs) for name, cs in sorted(variants.items())}
    touched: List[Tuple[str, ...]] = []
    for entry in mine_rows(root, train, list(tables.values()), limit=max_files):
        if entry.parents and not entry.truncated and entry.n_changes <= max_files:
            touched.append(tuple(path for status, path in entry.changes
                                 if status != "D" and countable_path(path)))
    known: Set[str] = set()
    if n_train:
        known = {path for path in ls_tree(root, shas[n_train - 1]) if countable_path(path)}
    counts: Dict[str, int] = {}
    for paths in (touched[-window:] if window else touched):
        for path in paths:
            if path in known:
                counts[path] = counts.get(path, 0) + 1
    order = sorted(known, key=lambda path: (-counts.get(path, 0), path))
    position = {path: i for i, path in enumerate(order)}
    by_dir: Dict[str, List[str]] = {}
    for path in order:
        by_dir.setdefault(path.rpartition("/")[0], []).append(path)

    methods = [*sorted(tables), *external, *BASELINES]
    neighbour_cache: Dict[Tuple[str, str], List[str]] = {}

    def ranked_list(method: str, seed: str) -> List[str]:
        key = (method, seed)
        hit = neighbour_cache.get(key)
        if hit is None:
            if method in tables:
                hit = [dst for dst, _n, _s in tables[method].neighbours(seed) if dst in known]
            elif method == "directory":
                hit = [p for p in by_dir.get(seed.rpartition("/")[0], ()) if p != seed]
            else:
                links = external[method][0].get(seed, {})
                hit = sorted((p for p in links if p in known and p != seed),
                             key=lambda p: (-links[p], p))
            neighbour_cache[key] = hit
        return hit

    skipped = {reason: 0 for reason in SKIP_REASONS}
    rows: Dict[str, List[dict]] = {m: [] for m in methods}
    commit_rows: List[dict] = []
    n_queries = 0
    gold_sizes: List[int] = []
    # No table is fed here: the commits after the cut are read and scored, never learned.
    for entry in mine_rows(root, test, (), limit=max_files):
        if entry.truncated or entry.n_changes > max_files:
            skipped["bulk"] += 1
            continue
        files = sorted({p for s, p in entry.changes if s != "D" and countable_path(p)})
        if len(files) < 2:
            skipped["single_file"] += 1
            continue
        seeds = [p for p in files if p in known]
        if len(seeds) < 2:
            skipped["unknown_files"] += 1
            continue
        per_method: Dict[str, List[dict]] = {m: [] for m in methods}
        for seed in seeds:
            gold = set(seeds)
            gold.discard(seed)
            n_queries += 1
            for method in methods:
                if method == "popularity":
                    at = position[seed]
                    ranks = [position[g] + (0 if position[g] > at else 1) for g in gold]
                    returned = len(order) - 1
                else:
                    ranked = ranked_list(method, seed)
                    ranks, returned = _list_ranks(ranked, gold), len(ranked)
                per_method[method].append(_score(ranks, len(gold), returned, ks))
        gold_sizes.append(len(seeds) - 1)
        for method in methods:
            rows[method].append(_mean_rows(per_method[method], ks))
        if per_commit:
            commit_rows.append({"commit": entry.sha, "seeds": len(seeds),
                                "rr": {m: rows[m][-1]["rr"] for m in methods}})

    n = len(gold_sizes)
    ci_k = 5 if 5 in ks else ks[-1]
    table: Dict[str, dict] = {}
    for method in methods:
        agg = aggregate(rows[method], ks)
        agg["coverage"] = (math.fsum(r["covered"] for r in rows[method]) / n) if n else 0.0
        agg["ci95"] = {
            f"recall_at_{ci_k}": list(bootstrap_ci(
                [r["recall_at"][ci_k] for r in rows[method]], resamples=resamples)),
            "mrr": list(bootstrap_ci([r["rr"] for r in rows[method]], resamples=resamples)),
        }
        table[method] = agg
    paired: Dict[str, dict] = {}
    contenders = [m for m in methods if m not in BASELINES]
    for name in contenders:
        paired[name] = {}
        for other in [*BASELINES, *(m for m in contenders if m != name)]:
            paired[name][other] = {
                "mrr": paired_bootstrap([r["rr"] for r in rows[name]],
                                        [r["rr"] for r in rows[other]], resamples=resamples),
                f"recall_at_{ci_k}": paired_bootstrap(
                    [r["recall_at"][ci_k] for r in rows[name]],
                    [r["recall_at"][ci_k] for r in rows[other]], resamples=resamples),
            }

    report: Dict[str, Any] = {
        "schema": HOLDOUT_SCHEMA,
        "repo": {"head": shas[-1] if shas else "", "cut": shas[n_train - 1] if n_train else ""},
        "settings": {
            "ks": list(ks), "max_files": max_files, "popularity_window": window,
            "cut_share": None if cut is not None else cut_share,
            "min_population": int(min_population), "resamples": resamples, "config": config,
            "cochange": {name: dataclasses.asdict(cs) for name, cs in sorted(variants.items())},
            "graphs": {name: dict(info, known_nodes=sum(1 for p in graph if p in known))
                       for name, (graph, info) in external.items()},
        },
        "population": {
            "commits": len(mainline), "train_commits": len(train), "test_commits": len(test),
            "eligible": n, "queries": n_queries, "skipped": skipped,
            "files_at_cut": len(known),
            "gold_files_mean": statistics.fmean(gold_sizes) if gold_sizes else 0.0,
            "gold_files_median": statistics.median(gold_sizes) if gold_sizes else 0,
            "conclusive": n >= int(min_population),
        },
        "train": {name: dict(tables[name].counts(), counted=tables[name].total,
                             bulk=tables[name].bulk,
                             edges=sum(len(tables[name].neighbours(p))
                                       for p in tables[name].files))
                  for name in sorted(tables)},
        "methods": table,
        "paired": paired,
    }
    if per_commit:
        report["per_commit"] = commit_rows
    return _rounded(report)


def _rounded(node):
    if isinstance(node, float):
        return round(node, 6)
    if isinstance(node, dict):
        return {key: _rounded(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_rounded(value) for value in node]
    return node


# -- text report -------------------------------------------------------------------

def format_holdout(report: dict) -> str:
    """The report as text. The population always comes first."""
    pop, cfg = report["population"], report["settings"]
    ks = cfg["ks"]
    lines = [
        f"Holdout  head={report['repo']['head'][:12] or '(none)'}  "
        f"cut={report['repo']['cut'][:12] or '(none)'}",
        f"Population: {pop['commits']:,} mainline commits; {pop['train_commits']:,} before the "
        f"cut (edges are built from these only), {pop['test_commits']:,} after",
        f"  scored: {pop['eligible']:,} commits, {pop['queries']:,} seed queries "
        f"(each file of a commit takes a turn as the seed)",
        "  skipped: " + (", ".join(f"{reason} {count:,}" for reason, count in
                                   sorted(pop["skipped"].items(), key=lambda kv: (-kv[1], kv[0]))
                                   if count) or "none"),
        f"  files at the cut: {pop['files_at_cut']:,}     other files to find per query: "
        f"mean {pop['gold_files_mean']:.1f}, median {pop['gold_files_median']:g}",
    ]
    for name, info in report["train"].items():
        lines.append(f"  {name}: {info['counted']:,} commits counted, {info['bulk']:,} bulk "
                     f"skipped, {info['pairs']:,} pairs, {info['edges']:,} edges")
    for name, info in cfg["graphs"].items():
        way = "directed" if info["directed"] else "read both ways"
        lines.append(f"  {name}: {info['edges']:,} edges over {info['nodes']:,} files "
                     f"({info['known_nodes']:,} of them exist at the cut; {way}; "
                     f"{info['rejected']:,} rows rejected). Not checked for leakage: "
                     f"build it from the tree at the cut")
    lines.append("")
    methods = report["methods"]
    if not pop["eligible"]:
        lines.append("  (no commit after the cut can be scored: nothing to report)")
        return "\n".join(lines) + "\n"
    width = max(len(name) for name in methods) + 2
    p_k = "5" if 5 in ks else str(ks[-1])
    head = "".join(f"{'R@' + str(k):<7}" for k in ks)
    lines.append(f"{'':<{width}}{head}{'P@' + p_k:<7}{'any@' + p_k:<7}{'MRR':<7}"
                 f"{'answered':<10}returned")
    for name, m in methods.items():
        cells = "".join(f"{m['recall_at'][str(k)]:<7.3f}" for k in ks)
        lines.append(f"{name:<{width}}{cells}{m['precision_at'][p_k]:<7.3f}"
                     f"{m['any_hit_at'][p_k]:<7.3f}{m['mrr']:<7.3f}"
                     f"{m['coverage']:<10.3f}{m['mean_returned']:.1f}")
    lines.append("(answered: share of queries for which the method returned anything; a method "
                 "that returns nothing scores zero on that query)")
    lines.append("")
    pair_width = max((len(f"{a} vs {b}") for a in report["paired"]
                      for b in report["paired"][a]), default=0) + 2
    for name, others in report["paired"].items():
        for other, cell in others.items():
            mrr = cell["mrr"]
            lines.append(f"{name + ' vs ' + other:<{pair_width}}dMRR {mrr['delta']:+.3f} "
                         f"[{mrr['ci95'][0]:+.3f}, {mrr['ci95'][1]:+.3f}]   better "
                         f"{mrr['better']} / worse {mrr['worse']} / tied {mrr['tied']}")
    if not pop["conclusive"]:
        lines.append(f"INCONCLUSIVE: {pop['eligible']} scored commits, fewer than the "
                     f"{cfg['min_population']} needed to call it either way")
    return "\n".join(lines).rstrip() + "\n"


# -- command line ------------------------------------------------------------------

def build_parser(prog: str = "carta graph holdout") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Temporal holdout for co-change edges: build them from the history before "
                    "a cut, then for each later commit predict its other files from one seed "
                    "file. Reads git objects only.")
    parser.add_argument("--repo", default=".", metavar="PATH",
                        help="Any path inside the repository (default: the current directory)")
    parser.add_argument("--rev", default="HEAD", metavar="REV",
                        help="Whose first-parent history to use (default HEAD)")
    parser.add_argument("--cut", default=None, metavar="REV",
                        help="The last training commit; it must be on that mainline")
    parser.add_argument("--cut-share", type=float, default=DEFAULT_CUT_SHARE, metavar="F",
                        help=f"Without --cut: train on this share of the commits "
                             f"(default {DEFAULT_CUT_SHARE})")
    parser.add_argument("--cochange", action="append", default=[],
                        metavar="NAME[=KEY:VALUE,...]",
                        help="A co-change variant to score, as overrides of graph.cochange "
                             "(for example lift=measure:lift or decay=half_life:200); "
                             "repeatable; default: one method with the configured settings")
    parser.add_argument("--graph", action="append", default=[], metavar="NAME=PATH",
                        help="An external file-to-file graph to score on the same queries "
                             "(JSON or TSV adjacency; repeatable)")
    parser.add_argument("--directed", action="store_true",
                        help="Read external graphs as directed (default: an edge counts "
                             "both ways)")
    parser.add_argument("--k", default=None, metavar="K[,K...]",
                        help="Cutoffs to report (default 1,3,5,10)")
    parser.add_argument("--max-files", type=int, default=None, metavar="N",
                        help="Skip commits touching more paths than this "
                             "(default: graph.cochange.max_files)")
    parser.add_argument("--popularity-window", type=int, default=0, metavar="N",
                        help="Training commits the popularity baseline counts (default 0: all)")
    parser.add_argument("--min-population", type=int, default=DEFAULT_MIN_POPULATION,
                        metavar="N", help="Below this many scored commits the result is "
                                          "inconclusive (default 30)")
    parser.add_argument("--no-config", action="store_true",
                        help="Ignore <repo>/.carta/config.yaml and use the built-in defaults")
    parser.add_argument("--resamples", type=int, default=RESAMPLES, metavar="N",
                        help=f"Bootstrap resamples (default {RESAMPLES})")
    parser.add_argument("--per-commit", action="store_true",
                        help="Include one row per scored commit in the JSON")
    parser.add_argument("--json", nargs="?", const="-", default=None, metavar="PATH",
                        help="Print the report as JSON, or write it to PATH and print the text")
    return parser


_VARIANT_FIELDS = {"max_files": int, "min_support": int, "min_score": float, "measure": str,
                   "half_life": int, "pending_window": int, "follow_renames": bool}


def parse_variants(specs: Sequence[str], base: CochangeSettings) -> Dict[str, CochangeSettings]:
    """``NAME=KEY:VALUE,...`` overrides of ``base``, validated like the config is."""
    from carta.graph.settings import graph_settings
    out: Dict[str, CochangeSettings] = {}
    for spec in specs:
        name, _sep, body = spec.partition("=")
        name = name.strip()
        if not name:
            raise HoldoutError(f"--cochange {spec!r}: expected NAME[=KEY:VALUE,...]")
        raw: Dict[str, Any] = dataclasses.asdict(base)
        for item in (part.strip() for part in body.split(",") if part.strip()):
            key, sep, value = item.partition(":")
            kind = _VARIANT_FIELDS.get(key.strip())
            if not sep or kind is None:
                raise HoldoutError(
                    f"--cochange {spec!r}: unknown setting {key.strip()!r} "
                    f"(known: {', '.join(sorted(_VARIANT_FIELDS))})")
            value = value.strip()
            try:
                if kind is bool:
                    if value not in ("true", "false"):
                        raise ValueError(value)
                    raw[key.strip()] = value == "true"
                else:
                    raw[key.strip()] = kind(value)
            except ValueError:
                raise HoldoutError(f"--cochange {spec!r}: bad value for {key.strip()}") from None
        out[name] = graph_settings({"graph": {"cochange": raw}}).cochange
    return out


def main(argv: Optional[Sequence[str]] = None, prog: str = "carta graph holdout") -> int:
    args = build_parser(prog).parse_args(argv)
    try:
        root = toplevel(Path(args.repo)) if Path(args.repo).is_dir() else None
        if root is None:
            raise HoldoutError(f"{args.repo} is not inside a git repository")
        ks = DEFAULT_KS
        if args.k:
            try:
                ks = tuple(int(part) for part in args.k.split(",") if part.strip())
            except ValueError:
                raise HoldoutError(f"--k {args.k!r}: expected positive integers") from None
        from carta.graph.eval.replay import _repo_settings
        settings, source = _repo_settings(root, (), (), use_config=not args.no_config)
        variants = parse_variants(args.cochange, settings.cochange) or None
        graphs = {}
        for spec in args.graph:
            name, sep, path = spec.partition("=")
            if not sep or not name.strip() or not path.strip():
                raise HoldoutError(f"--graph {spec!r}: expected NAME=PATH")
            graphs[name.strip()] = load_external_graph(Path(path.strip()),
                                                       directed=args.directed)
        report = run_holdout(
            root, rev=args.rev, cut=args.cut, cut_share=args.cut_share, ks=ks,
            settings=settings, variants=variants, graphs=graphs, max_files=args.max_files,
            popularity_window=args.popularity_window, min_population=args.min_population,
            resamples=args.resamples, per_commit=args.per_commit, config=source)
        if args.json and args.json != "-":
            out = Path(args.json)
            try:
                write_text_replacing(out, json.dumps(report, indent=2, sort_keys=True) + "\n")
            except OSError as exc:
                raise HoldoutError(f"could not write the report to {out}: {exc}") from exc
    except (HoldoutError, ConfigError, GitError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.json == "-":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        sys.stdout.write(format_holdout(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(prog="python -m carta.graph.eval.holdout"))
