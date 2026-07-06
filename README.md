# KWIM

KWIM is a stack for running teams of AI agents with shared foundational
knowledge. It is an implementation of CoALA as described in the arXiv paper
https://arxiv.org/abs/2309.02427 with some additional features and 
enhancements. KWIM components map out as **K**nowledge, **W**isdom,
**I**ntelligence, **M**emory, plus an additional tooling layer (**+T**)
KWIM is intended to provide a shared base for an agent team so that an
individual agent's learning and experiences can benefit the entire team.
The stack is built around existing off-the-shelf software typically comprised
of a graph database (FalkorDB), a relational database (Postgres), a model
gateway (LiteLLM), and an internal message bus (RabbitMQ). KWIM is the substrate
that unites them into one governed, multi-agent system.

The pieces:

- **Knowledge** - facts stored as graph relationships, with provenance, retraction,
  and freshness/decay
- **Wisdom** - advice as hints ("when X, do Y") and rules as hard constraints ("never run Z").
- **Intelligence** - the model gateway, handles routing and accounting, where usage 
  and cost can be tracked per team or per agent
- **Memory** - episodic memory, an append-only log of events used as a feedback source
  for (K) and (W)
- **+T** - gated tooling to provide additional capabilities

Agents reach Knowledge, Wisdom, and Memory through the **kwim-service**, a plain
HTTP/JSON API providing the interface for those components. Intelligence is 
provided as a separate gateway used directly for model inference. These components 
are implemented as off-the-shelf software as much as possible with most of the 
customization work focused on integrating the stack.

## How the kwim-service works

The kwim-service is the API to Knowledge, Wisdom, and Memory. The parts worth
knowing:

**Agents don't write facts directly - they propose them.** Every proposal goes
through a gate that checks it against what's already stored: duplicates get
dropped, contradictions get flagged for a human to decide, everything else
is auto-committed. Facts are not edited or overwritten but are superseded
so the history stays intact.

**The source of truth is the append-only log in Postgres, not the graph.** The graph
(FalkorDB) that agents query is a view built by replaying that log. The graph can be
rebuilt at any time by replaying the log to get it back. 

**Teams are isolated by their API key.** A team only ever sees its own data plus a
shared "universe" of globally approved rules. There's no way to ask for another team's data
through the API.

**Agents warm-start from the kwim-service** Before working, an agent asks for what's 
relevant and gets back the knowledge, rules, and recent events that matter, along
with honest markers for what's *missing* so the agent knows when it's working
blind instead of guessing.

## Tooling (the +T)

On top of K/W/I/M, KWIM adds gated tools - the +T. This includes a code graph where the
kwim-service has parsed relevant repositories into a graph of functions, calls, and
imports, so agents can ask "what calls this?" or "what would this change break?"
without reading the whole codebase. Like everything else, those tool calls are
governed and logged. The code graph is based on Codebase-Memory from this arXiv
paper: https://arxiv.org/abs/2603.27277

## Documentation

- [Deploying KWIM](docs/deployment.md) - prerequisites and the step-by-step bring-up
  order for standing up the full stack.
- [Operating KWIM](docs/operations.md) - day-2: provisioning teams, adding code-graph
  repos, governed cleanup/forget, rebuilds, and the failure modes worth knowing.

## Layout

| Path | What's in it |
|------|--------------|
| `service/` | The kwim-service - FastAPI app, the Postgres and FalkorDB stores, the gate, freshness/decay, graph rebuild, and the code-graph extractor. Plus its tests. |
| `db/` | Per-team schema template and rendered SQL. |
| `k8s/` | Example Kubernetes manifests for running it. |
| `tools/` | Operational tools (e.g. audit-log queries). |

## Notes

The code and tests are the real description of how the kwim-service behaves - read
those if a doc and the code disagree.
