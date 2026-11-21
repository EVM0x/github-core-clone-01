
# --------------------------------------------------------------------------- MCP (streamable HTTP)

MCP_VERSION = "2025-06-18"
# Revisions this server speaks, newest first. server/discover reports the list;
# a client that only knows an older handshake still gets an answer from initialize.
MCP_REVISIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"]


def mcp_tools():
    def obj(properties, required):
        return {"type": "object", "properties": properties, "required": required,
                "additionalProperties": False}

    owner_repo = {"owner": {"type": "string", "description": "repository owner login"},
                  "repo": {"type": "string", "description": "repository name"}}
    return [
        {"name": "create_repo", "description": "Create a repository for the authenticated user.",
         "inputSchema": obj({"name": {"type": "string", "description": "repository name"},
                             "description": {"type": "string", "description": "short description"},
                             "private": {"type": "boolean", "description": "private when true"}},
                            ["name"])},
        {"name": "get_repo", "description": "Read one repository.",
         "inputSchema": obj(dict(owner_repo), ["owner", "repo"])},
        {"name": "create_issue", "description": "Open an issue on a repository.",
         "inputSchema": obj(dict(owner_repo,
                                 title={"type": "string", "description": "issue title"},
                                 body={"type": "string", "description": "issue body"}),
                            ["owner", "repo", "title"])},
        {"name": "list_issues", "description": "List a repository's issues.",
         "inputSchema": obj(dict(owner_repo,
                                 state={"type": "string", "enum": ["open", "closed", "all"],
                                        "description": "issue state filter"}),
                            ["owner", "repo"])},
        {"name": "create_pull_request", "description": "Open a pull request from head into base.",
         "inputSchema": obj(dict(owner_repo,
                                 title={"type": "string", "description": "pull request title"},
                                 body={"type": "string", "description": "pull request body"},
                                 head={"type": "string", "description": "source branch"},
                                 base={"type": "string", "description": "target branch"}),
                            ["owner", "repo", "head", "base"])},
        {"name": "merge_pull_request", "description": "Merge a pull request into its base branch.",
         "inputSchema": obj(dict(owner_repo,
                                 pull_number={"type": "integer", "description": "pull request number"}),
                            ["owner", "repo", "pull_number"])},
    ]


TOOLS = mcp_tools()
TOOL_INDEX = {tool["name"]: tool for tool in TOOLS}


def resolve_tool_name(name):
    """A server prefix is allowed and the match is case-insensitive.

    github__create_issue and GitHub__Create_Issue both mean create_issue, which is
    what github-mcp-server sends when it is running behind a proxy.
    """
    if not isinstance(name, str) or not name:
        return None
    if name in TOOL_INDEX:
        return name
    wanted = name.lower()
    for candidate in TOOL_INDEX:
        if wanted == candidate.lower():
            return candidate
    suffix = wanted.rsplit("__", 1)[-1]
    for candidate in TOOL_INDEX:
        if suffix == candidate.lower():
            return candidate
    return None


def mcp_text(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
            "isError": is_error}


def mcp_call(name, args):
    """Run one tool call against the same store the REST surface reads."""
    tool = TOOL_INDEX.get(name)
    if tool is None:
        return mcp_text({"error": "unknown tool: %s" % name}, True), None
    if not isinstance(args, dict):
        return mcp_text({"error": "arguments must be an object"}, True), None
    missing = [field for field in tool["inputSchema"]["required"] if args.get(field) in (None, "")]
    if missing:
        return mcp_text({"error": "missing required argument(s): " + ", ".join(missing)}, True), None
    try:
        if name == "create_repo":
            status, payload, _headers = create_repo({"name": args["name"],
                                                     "description": args.get("description") or "",
                                                     "private": bool(args.get("private"))})
            return mcp_text(payload), None
        repo = need_repo(args["owner"], args["repo"])
        if name == "get_repo":
            return mcp_text(repo_json(repo)), None
        if name == "create_issue":
            _status, payload, _headers = create_issue(repo, {"title": args["title"],
                                                             "body": args.get("body") or ""})
            return mcp_text(payload), None
        if name == "list_issues":
            wanted = args.get("state") or "open"
            items = [issue_json(repo, issue) for issue in repo.issues
                     if wanted == "all" or issue["state"] == wanted]
            return mcp_text(items), None
        if name == "create_pull_request":
            _status, payload, _headers = create_pull(repo, {"title": args.get("title") or "",
                                                            "body": args.get("body") or "",
                                                            "head": args["head"],
                                                            "base": args["base"]})
            return mcp_text(payload), None
        if name == "merge_pull_request":
            pull = find_pull(repo, args["pull_number"])
            if pull is None:
                return mcp_text({"error": "no pull request #%s" % args["pull_number"]}, True), None
            _status, payload, _headers = merge_pull(repo, pull, {})
            payload = dict(payload)
            payload["number"] = pull["number"]
            return mcp_text(payload), None
    except HttpError as err:
        return mcp_text({"error": err.message, "status": err.status}, True), None
    return mcp_text({"error": "unknown tool: %s" % name}, True), None


def mcp_handle(payload):
    """One JSON-RPC message in, one response object (or None for a notification) out."""
    if not isinstance(payload, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    if not isinstance(method, str):
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32600, "message": "Invalid Request"}}
    if request_id is None:
        return None  # a notification never gets a response
    if method == "initialize":
        offered = params.get("protocolVersion")
        version = offered if isinstance(offered, str) and offered else MCP_VERSION
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"protocolVersion": version,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "forge", "version": "1.0.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "server/discover":
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"supportedVersions": list(MCP_REVISIONS),
                           "protocolVersion": MCP_REVISIONS[0],
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "forge", "version": "1.0.0"}}}
    if method == "tools/call":
        name = resolve_tool_name(params.get("name"))
        if name is None:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": "Unknown tool: %s" % (params.get("name") or "")}}
        result, _ = mcp_call(name, params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": "Method not found: %s" % method}}


def mcp_dispatch(self, method, body_raw):
    if not self.headers.get("Authorization"):
        self.send_json(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
        return
    if method == "GET":
        self.send_json(405, {"error": "method not allowed"}, {"Allow": "POST"})
        return
    if method != "POST":
        self.send_json(405, {"error": "method not allowed"}, {"Allow": "POST"})
        return
    try:
        payload = json.loads(body_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        self.send_json(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})
        return
    response = mcp_handle(payload)
    if response is None:
        self.send_bytes(202, b"", "application/json")
        return
    self.send_json(200, response)


Handler.mcp = mcp_dispatch
