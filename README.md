# forge — a GitHub-compatible git forge in one process

Entry for make.fun's `github-core-clone-01`. One Python 3.13 process, standard
library only, no git binary, no UI, its own on-disk store. It binds an ephemeral
port, registers it with the suite at `$MAKEFUN_ENV_URL/register`, then long-polls
`/done` and exits when the suite says the trial is over.

## Surfaces

| surface | path | notes |
|---|---|---|
| git smart HTTP | `/{owner}/{repo}.git/...` | upload-pack protocol v2, receive-pack v0; pack v2 with deltas, ref deltas, shallow and `deepen`, non-fast-forward refusal per ref |
| REST v3 | `/api/v3/...` | repos, contents, issues, labels, pull requests, merge, pagination via `Link: rel="next"` |
| GraphQL v4 | `/api/graphql` | hand-written lexer + parser, introspection for `Issue`, `PullRequest`, `Repository`, `SearchType`, ordered issue connections, mutations |
| MCP | `/mcp` | JSON-RPC 2.0 over streamable HTTP, `server/discover`, `initialize`, `tools/list`, six tools that act on the REST store |

## Layout

    src/10_head.py      imports, store, ids, json helpers
    src/20_model.py     objects: repos, issues, pulls, labels, users
    src/30_rest.py      REST v3 routing and handlers
    src/35_issues.py    issues, labels, comments, pulls, merge
    src/40_http.py      the HTTP server, routing table, error mapping
    src/44_stub.py      placeholder kept for build ordering
    src/50_githttp.py   smart HTTP: pkt-line framing, packs, deltas, refs
    src/60_mcp.py       MCP transport and tools
    src/70_graphql.py   GraphQL lexer, parser, executor, introspection
    src/90_main.py      lifecycle: bind, register, serve, drain

`./build.sh` concatenates `src/*.py` into the single-file entrypoint `main.py`
that the trial runs (`python3 /bot/main.py`). `./run-suite.sh` starts it on a
fresh store and runs the kit's own conformance suite against it.

## Determinism

State is append-only and keyed by content: object ids are sha1 of their bytes,
issue and repository ids come from a counter that starts at a fixed value, and
no timestamp, port or wall clock is written into the exported state. Two runs on
the same instance leave byte-identical exports.

## License

MIT.
