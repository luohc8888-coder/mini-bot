import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const graph = JSON.parse(
  readFileSync(resolve(here, "../src/mocks/project-graph.json"), "utf8"),
);

assert.equal(typeof graph.project?.name, "string");
assert.ok(Array.isArray(graph.nodes) && graph.nodes.length > 0);
assert.ok(Array.isArray(graph.edges) && graph.edges.length > 0);

const byId = new Map();
for (const node of graph.nodes) {
  assert.equal(typeof node.id, "string");
  assert.ok(!byId.has(node.id), `duplicate node id: ${node.id}`);
  byId.set(node.id, node);
}

const allowedParents = {
  module: [null],
  class: ["module"],
  function: ["module"],
  method: ["class"],
};

for (const node of graph.nodes) {
  assert.ok(allowedParents[node.kind], `unknown node kind: ${node.kind}`);
  const parent = node.parentId === null ? null : byId.get(node.parentId);
  if (node.parentId !== null) {
    assert.ok(parent, `missing parent for ${node.id}: ${node.parentId}`);
  }
  const expected = allowedParents[node.kind];
  assert.ok(
    expected.includes(parent?.kind ?? null),
    `invalid parent kind for ${node.id}: ${parent?.kind ?? null}`,
  );
  if (node.kind === "function" || node.kind === "method") {
    assert.equal(typeof node.filePath, "string", `missing filePath: ${node.id}`);
    assert.equal(typeof node.startLine, "number", `missing startLine: ${node.id}`);
    assert.equal(typeof node.endLine, "number", `missing endLine: ${node.id}`);
  }
}

for (const edge of graph.edges) {
  assert.ok(byId.has(edge.source), `missing edge source: ${edge.source}`);
  assert.ok(byId.has(edge.target), `missing edge target: ${edge.target}`);
  assert.ok(
    ["contains", "imports", "calls"].includes(edge.kind),
    `unknown edge kind: ${edge.kind}`,
  );
  assert.ok(
    ["static", "inferred"].includes(edge.confidence),
    `unknown confidence: ${edge.confidence}`,
  );
}

const rootModules = graph.nodes.filter(
  (node) => node.kind === "module" && node.parentId === null,
);
assert.ok(rootModules.length >= 4, "expected at least 4 root modules");

const childCounts = new Map();
for (const node of graph.nodes) {
  if (node.parentId) {
    childCounts.set(node.parentId, (childCounts.get(node.parentId) ?? 0) + 1);
  }
}
for (const node of graph.nodes) {
  if (node.kind === "module" || node.kind === "class") {
    assert.ok(childCounts.get(node.id) > 0, `container has no children: ${node.id}`);
  }
}

const kinds = new Set(graph.nodes.map((node) => node.kind));
for (const kind of ["module", "class", "function", "method"]) {
  assert.ok(kinds.has(kind), `missing node kind: ${kind}`);
}

console.log(
  `Mock graph OK: ${graph.nodes.length} nodes, ${graph.edges.length} edges, ` +
    `${rootModules.length} root modules`,
);
