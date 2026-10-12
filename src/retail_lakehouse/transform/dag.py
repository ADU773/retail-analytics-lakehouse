"""Dependency graph of models: validation, topological sort, cycle detection, selection.

Selection syntax (dbt-like):

* ``silver.orders``      - exactly one model (bare ``orders`` also works when unambiguous)
* ``gold.mart_*``        - shell-style glob
* ``+gold.mart_rfm``     - the model plus everything upstream
* ``silver.orders+``     - the model plus everything downstream
* ``+gold.fact_orders+`` - both directions
"""

from __future__ import annotations

import fnmatch
import heapq
from collections.abc import Iterable

from retail_lakehouse.transform.models import Model, referenced_tables

LAYER_RANK = {"bronze": 0, "silver": 1, "quarantine": 1, "gold": 2, "meta": 3}


class DagError(ValueError):
    """Invalid graph or selection."""


class CycleError(DagError):
    """The dependency graph contains a cycle."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__("dependency cycle detected: " + " -> ".join(cycle))


class Dag:
    """Directed acyclic graph of models keyed by qualified name."""

    def __init__(self, models: dict[str, Model], sources: Iterable[str] = ()) -> None:
        self.models = models
        self.sources = set(sources)
        # quarantine tables are produced by their parent model
        self.produced_by = {m.quarantine: m.name for m in models.values() if m.quarantine}
        self.parents: dict[str, set[str]] = {name: set() for name in models}
        self.children: dict[str, set[str]] = {name: set() for name in models}
        for model in models.values():
            for dep in model.depends_on:
                target = self.produced_by.get(dep, dep)
                if target in models:
                    if target == model.name:
                        raise CycleError([model.name, model.name])
                    self.parents[model.name].add(target)
                    self.children[target].add(model.name)
                elif dep not in self.sources:
                    raise DagError(f"{model.name} depends on unknown model or source {dep!r}")
            self._check_undeclared(model)

    def _check_undeclared(self, model: Model) -> None:
        """Every model/quarantine table referenced in SQL must appear in ``depends_on``."""
        if model.kind != "sql" or not model.sql:
            return
        declared = set(model.depends_on) | {self.produced_by.get(d, d) for d in model.depends_on}
        own = {model.name, model.quarantine}
        for ref in sorted(referenced_tables(model.sql) - own):
            known = ref in self.models or ref in self.produced_by or ref in self.sources
            if known and ref not in declared:
                raise DagError(
                    f"{model.name} references {ref} but does not declare it in '-- depends_on:'"
                )

    # ------------------------------------------------------------------ ordering
    def _key(self, name: str) -> tuple[int, str]:
        return (LAYER_RANK.get(name.split(".", 1)[0], 9), name)

    def topological_order(self) -> list[str]:
        """Kahn's algorithm with a deterministic (layer, name) tie-break."""
        indegree = {n: len(p) for n, p in self.parents.items()}
        ready = [self._key(n) for n, d in indegree.items() if d == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            _, name = heapq.heappop(ready)
            order.append(name)
            for child in self.children[name]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, self._key(child))
        if len(order) != len(self.models):
            raise CycleError(self.find_cycle() or sorted(set(self.models) - set(order)))
        return order

    def find_cycle(self) -> list[str]:
        """Return one cycle as ``[a, b, ..., a]`` (empty list when acyclic)."""
        white, grey, black = 0, 1, 2
        color = dict.fromkeys(self.models, white)
        stack: list[str] = []

        def visit(node: str) -> list[str]:
            color[node] = grey
            stack.append(node)
            for nxt in sorted(self.children[node]):
                if color[nxt] == grey:
                    return [*stack[stack.index(nxt) :], nxt]
                if color[nxt] == white:
                    found = visit(nxt)
                    if found:
                        return found
            stack.pop()
            color[node] = black
            return []

        for node in sorted(self.models):
            if color[node] == white:
                cycle = visit(node)
                if cycle:
                    return cycle
        return []

    # ------------------------------------------------------------------ traversal
    def _walk(self, start: str, edges: dict[str, set[str]]) -> set[str]:
        seen: set[str] = set()
        todo = list(edges[start])
        while todo:
            node = todo.pop()
            if node not in seen:
                seen.add(node)
                todo.extend(edges[node])
        return seen

    def ancestors(self, name: str) -> set[str]:
        return self._walk(name, self.parents)

    def descendants(self, name: str) -> set[str]:
        return self._walk(name, self.children)

    def _match(self, pattern: str) -> set[str]:
        if pattern in self.models:
            return {pattern}
        hits = {n for n in self.models if fnmatch.fnmatch(n, pattern)}
        if not hits and "." not in pattern:
            hits = {n for n in self.models if fnmatch.fnmatch(n.split(".", 1)[1], pattern)}
        return hits

    def select(self, selectors: Iterable[str]) -> list[str]:
        """Resolve selectors to a topologically ordered list of model names."""
        chosen: set[str] = set()
        tokens = [t for s in selectors for t in s.replace(",", " ").split()]
        if not tokens:
            raise DagError("empty selection")
        for token in tokens:
            up, down = token.startswith("+"), token.endswith("+")
            pattern = token.strip("+").lower()
            hits = self._match(pattern)
            if not hits:
                raise DagError(f"selector {token!r} matched no models")
            for hit in hits:
                chosen.add(hit)
                if up:
                    chosen |= self.ancestors(hit)
                if down:
                    chosen |= self.descendants(hit)
        return [n for n in self.topological_order() if n in chosen]
