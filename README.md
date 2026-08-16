<h1 align="center">KWIM</h1>

<p align="center">
  <em>Knowledge, Wisdom, Intelligence, Memory - a governed memory substrate for teams of AI agents.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/Kubernetes-326CE5?logo=kubernetes&logoColor=white" alt="Kubernetes">
  <img src="https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/OpenTelemetry-425CC7?logo=opentelemetry&logoColor=white" alt="OpenTelemetry">
  <a href="https://arxiv.org/abs/2309.02427"><img src="https://img.shields.io/badge/arXiv-2309.02427-b31b1b?logo=arxiv&logoColor=white" alt="CoALA paper"></a>
</p>

KWIM is a stack for running teams of AI agents with shared foundational
knowledge. It is an implementation of CoALA as described in the arXiv paper
https://arxiv.org/abs/2309.02427 with some additional features and 
enhancements. 

KWIM is intended to provide a shared base for an agent team so that an
individual agent's learning and experiences can benefit the entire team.
KWIM components map out as **K**nowledge, **W**isdom, **I**ntelligence, and
**M**emory, with a tooling layer (**+T**) to provide gated MCP usage.
The stack is built around existing off-the-shelf software typically comprised
of a graph database (FalkorDB), a relational database (Postgres), a model
gateway (LiteLLM), and an internal message bus (RabbitMQ). KWIM is the substrate
that unites them into one governed, multi-agent system.

The pieces:

- **Knowledge** - facts stored as graph relationships, with provenance, retraction,
  and freshness/decay. Readable two ways: by structured filter when the caller
  already knows what it is looking for, and by semantic search when it does not.
  Facts arrive from two sources: agents proposing what they learned, and an
  extractor that reads your repositories into a code graph and proposes what it
  finds there. See "Knowledge from code" below.
- **Wisdom** - advice as hints ("when X, do Y") and rules as hard constraints ("never run Z").
- **Intelligence** - the model gateway, handles routing and accounting, where usage 
  and cost can be tracked per team or per agent
- **Memory** - three kinds. Episodic is an append-only log of events, and the feedback
  source for (K) and (W). Semantic is a vector index for recall by meaning rather than
  by name. Working is short-lived per-session scratch, deliberately discarded.
- **+T** - the tool layer, for acting on systems outside KWIM.

Agents reach Knowledge, Wisdom, and Memory through the kwim-service, a plain
HTTP/JSON API providing the interface for those components. Intelligence is 
provided as a separate gateway used directly for model inference. These components 
are implemented as off-the-shelf software as much as possible with most of the 
customization work focused on integrating the stack.

## How the kwim-service works

The kwim-service is the API to Knowledge, Wisdom, and Memory. The parts worth
knowing:

**Agents don't write facts directly - they propose them.** Every proposal goes
through a gate. For a fact, the gate embeds the statement and measures it against
what is already stored: a near-identical match is rejected as a duplicate, a merely
close one goes to a human as a possible conflict or supersession, and the rest
commit automatically. Proposals citing evidence the system doesn't recognise, or
resting on too little of it, also go to a human. Worth being precise about what this
is: the gate measures how close two statements are, not whether they disagree.

**History is preserved by default and destroyed only deliberately.** A new fact
supersedes the one it replaces rather than overwriting it, and a retraction is a
status change, not a deletion, so the chain stays walkable and "why did we believe
this in March?" remains answerable. The one exception is the governed forget path,
which is a real hard delete for content that must not survive at all.

**The source of truth is the append-only log in Postgres, not the graph.** The graph
(FalkorDB) that agents query is a view built by replaying that log. The graph can be
rebuilt at any time by replaying the log to get it back.

**Teams are isolated by their API key.** A team only ever sees its own data plus a
shared "universe" of globally approved rules. There's no way to ask for another team's data
through the API.

**Agents warm-start from the kwim-service** Before working, an agent asks for what's 
relevant and gets back the knowledge, rules, recent events, and relevant code that
matter, along with honest markers for what's missing so the agent knows when it's
working blind instead of guessing. The knowledge it gets back is matched both by tag
and by meaning, so a subject the agent could not have named exactly still retrieves.

## Knowledge from code

An extractor parses your Python repositories into a code graph - functions, the
calls between them, and imports - held in its own graph per team. Agents query it
directly to ask "what calls this?" (an inbound call trace) or "what would this
change break?" (the files whose indexed commit has moved on), without reading the
whole codebase. Warm-start packs a slice of it too, so an agent starts with the
code relevant to its subject.

Those queries are read-only, and each one lands in episodic memory, so what an
agent looked at is part of the record. On top of that a distiller derives durable
claims from the graph - a per-repo architecture summary, cross-repo interfaces -
and proposes them through the same gate every other proposer uses. That is why
this sits under Knowledge rather than under tooling: what it produces is facts,
governed exactly like any other fact.

The code graph is the one store that does not replay from the commit log. Its
source of truth is the repositories themselves, so it is refreshed by re-running
the extractor, and a rebuild deliberately leaves it alone. It is based on
Codebase-Memory from this arXiv paper: https://arxiv.org/abs/2603.27277

## Tooling (+T)

The tool layer is where agents act on systems outside KWIM - a Kubernetes
control plane, a database, whatever else a team needs reached through MCP. It is
reserved but not yet implemented: currently, nothing exposes a tool surface today.

Some of the groundwork is already in place, such as capability-scoped API keys
to gate the high-bar operations, which is the shape tool authorization wants.

## Documentation

- [The contract](docs/contract.md) - the HTTP/JSON surface every agent codes against,
  and the one thing to read if you are integrating.
- [Data model](docs/data-model.md) - what is stored where, and why.
- [Deploying KWIM](docs/deployment.md) - prerequisites and the step-by-step bring-up
  order for standing up the full stack.
- [Operating KWIM](docs/operations.md) - day-2: provisioning teams, adding code-graph
  repos, governed cleanup/forget, rebuilds, and the failure modes worth knowing.

## Layout

| Path | What's in it |
|------|--------------|
| `service/` | The kwim-service - FastAPI app, the Postgres and FalkorDB stores, the gate, freshness/decay, graph rebuild, and the code-graph extractor. Plus its tests. |
| `services/` | The distiller - a scheduled job that reads a team's episodic events, extracts durable learnings from them, and proposes those back through the gate like any other client. |
| `clients/` | The Python client agents use to reach the kwim-service, plus model routing and secret reading. |
| `db/` | Per-team schema template and rendered SQL. |
| `k8s/` | Example Kubernetes manifests for running it. |

## Notes

The code and tests are the real description of how the kwim-service behaves - read
those if a doc and the code disagree.
