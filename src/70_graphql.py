
# --------------------------------------------------------------------------- GraphQL v4

class GQLError(Exception):
    pass


GQL_PUNCT = set("{}()[]:,!$=@|&")


def gql_tokens(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char in " \t\r\n,":
            i += 1
            continue
        if char == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("...", i):
            tokens.append(("punct", "..."))
            i += 3
            continue
        if char in GQL_PUNCT:
            tokens.append(("punct", char))
            i += 1
            continue
        if char == '"':
            if text.startswith('"""', i):
                end = text.find('"""', i + 3)
                if end < 0:
                    raise GQLError("unterminated block string")
                tokens.append(("string", text[i + 3 : end]))
                i = end + 3
                continue
            i += 1
            out = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    escape = text[i + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(escape, escape))
                    i += 2
                    continue
                out.append(text[i])
                i += 1
            if i >= n:
                raise GQLError("unterminated string")
            tokens.append(("string", "".join(out)))
            i += 1
            continue
        if char.isdigit() or (char == "-" and i + 1 < n and text[i + 1].isdigit()):
            start = i
            i += 1
            while i < n and (text[i].isdigit() or text[i] in ".eE+-"):
                i += 1
            raw = text[start:i]
            tokens.append(("number", float(raw) if any(c in raw for c in ".eE") else int(raw)))
            continue
        if char.isalpha() or char == "_":
            start = i
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            tokens.append(("name", text[start:i]))
            continue
        raise GQLError("unexpected character %r" % char)
    return tokens


class GQLParser:
    def __init__(self, text):
        self.tokens = gql_tokens(text)
        self.pos = 0
        self.fragments = {}

    def peek(self, offset=0):
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else (None, None)

    def next(self):
        token = self.peek()
        self.pos += 1
        return token

    def accept(self, kind, value=None):
        token = self.peek()
        if token[0] == kind and (value is None or token[1] == value):
            self.pos += 1
            return token
        return None

    def expect(self, kind, value=None):
        token = self.accept(kind, value)
        if token is None:
            raise GQLError("expected %s %s, saw %r" % (kind, value or "", self.peek()[1]))
        return token

    def parse_document(self):
        operations = []
        while self.pos < len(self.tokens):
            token = self.peek()
            # Anonymous shorthand: a document that opens on a selection set is a
            # query with no name and no variables. gh and the MCP server both
            # send introspection this way.
            if token[0] == "punct" and token[1] == "{":
                operations.append({"type": "query", "name": None, "variables": {},
                                   "selections": self.parse_selection_set(),
                                   "directives": [], "fragments": self.fragments})
                continue
            if token[0] != "name":
                raise GQLError("expected an operation")
            keyword = token[1]
            if keyword == "fragment":
                self.next()
                name = self.expect("name")[1]
                self.expect("name", "on")
                self.expect("name")
                self.fragments[name] = self.parse_selection_set()
                continue
            self.next()
            operation_type = keyword if keyword in ("query", "mutation", "subscription") else "query"
            if keyword not in ("query", "mutation", "subscription"):
                self.pos -= 1
            name = None
            if self.peek()[0] == "name":
                name = self.next()[1]
            variables = {}
            if self.accept("punct", "("):
                while not self.accept("punct", ")"):
                    self.expect("punct", "$")
                    var_name = self.expect("name")[1]
                    self.expect("punct", ":")
                    type_name = self.parse_type()
                    default = None
                    if self.accept("punct", "="):
                        default = self.parse_value()
                    variables[var_name] = {"type": type_name, "default": default}
            directives = self.parse_directives()
            selections = self.parse_selection_set()
            operations.append({"type": operation_type, "name": name, "variables": variables,
                               "selections": selections, "directives": directives,
                               "fragments": self.fragments})
        return operations

    def parse_type(self):
        if self.accept("punct", "["):
            inner = self.parse_type()
            self.expect("punct", "]")
            name = "[" + inner + "]"
        else:
            name = self.expect("name")[1]
        if self.accept("punct", "!"):
            name += "!"
        return name

    def parse_directives(self):
        out = []
        while self.accept("punct", "@"):
            name = self.expect("name")[1]
            args = self.parse_arguments() if self.peek()[1] == "(" else {}
            out.append((name, args))
        return out

    def parse_arguments(self):
        args = {}
        self.expect("punct", "(")
        while not self.accept("punct", ")"):
            name = self.expect("name")[1]
            self.expect("punct", ":")
            args[name] = self.parse_value()
        return args

    def parse_value(self):
        token = self.peek()
        if token[0] == "punct" and token[1] == "$":
            self.next()
            return ("var", self.expect("name")[1])
        if token[0] == "punct" and token[1] == "[":
            self.next()
            items = []
            while not self.accept("punct", "]"):
                items.append(self.parse_value())
            return ("list", items)
        if token[0] == "punct" and token[1] == "{":
            self.next()
            fields = {}
            while not self.accept("punct", "}"):
                key = self.expect("name")[1]
                self.expect("punct", ":")
                fields[key] = self.parse_value()
            return ("object", fields)
        if token[0] == "string":
            self.next()
            return ("literal", token[1])
        if token[0] == "number":
            self.next()
            return ("literal", token[1])
        if token[0] == "name":
            self.next()
            if token[1] == "true":
                return ("literal", True)
            if token[1] == "false":
                return ("literal", False)
            if token[1] == "null":
                return ("literal", None)
            return ("enum", token[1])
        raise GQLError("bad value")

    def parse_selection_set(self):
        self.expect("punct", "{")
        selections = []
        while not self.accept("punct", "}"):
            if self.accept("punct", "..."):
                if self.peek()[0] == "name" and self.peek()[1] == "on":
                    self.next()
                    type_condition = self.expect("name")[1]
                    selections.append({"kind": "inline", "type": type_condition,
                                       "selections": self.parse_selection_set()})
                else:
                    name = self.expect("name")[1]
                    selections.append({"kind": "fragment", "name": name})
                continue
            first = self.expect("name")[1]
            alias = None
            name = first
            if self.accept("punct", ":"):
                alias = first
                name = self.expect("name")[1]
            args = self.parse_arguments() if self.peek()[1] == "(" else {}
            self.parse_directives()
            sub = self.parse_selection_set() if self.peek()[1] == "{" else []
            selections.append({"kind": "field", "alias": alias, "name": name,
                               "args": args, "selections": sub})
        return selections


def gql_value(node, variables):
    kind, payload = node
    if kind == "var":
        return variables.get(payload)
    if kind == "list":
        return [gql_value(item, variables) for item in payload]
    if kind == "object":
        return {key: gql_value(value, variables) for key, value in payload.items()}
    if kind == "enum":
        return payload
    return payload


def expand(selections, variables, fragments=None):
    fragments = fragments or {}
    out = []
    for selection in selections:
        if selection["kind"] == "fragment":
            out.extend(expand(fragments.get(selection["name"], []), variables, fragments))
        elif selection["kind"] == "inline":
            out.append(selection)
        else:
            out.append(selection)
    return out


# -- schema ------------------------------------------------------------------

REPO_FIELDS = ["id", "databaseId", "name", "nameWithOwner", "owner", "description", "isPrivate",
               "defaultBranchRef", "viewerPermission", "hasIssuesEnabled", "hasPullRequestsEnabled",
               "issues", "issue", "pullRequests", "pullRequest", "createdAt", "updatedAt", "url",
               "isFork", "stargazerCount", "forkCount", "visibility", "isEmpty"]
ISSUE_FIELDS = ["id", "number", "title", "body", "state", "stateReason", "createdAt", "updatedAt",
                "closedAt", "author", "repository", "labels", "assignees", "comments", "milestone",
                "url", "isPinned", "locked", "includesCreatedEdit", "resourcePath", "bodyText"]
PR_FIELDS = ["id", "number", "title", "body", "state", "headRefName", "baseRefName", "mergeable",
             "merged", "mergedAt", "isDraft", "createdAt", "updatedAt", "author", "repository",
             "files", "commits", "url", "reviewDecision", "additions", "deletions",
             "changedFiles", "headRefOid", "baseRefOid"]
SEARCH_ENUMS = ["ISSUE", "ISSUE_ADVANCED", "REPOSITORY", "USER", "DISCUSSION"]
TYPE_FIELDS = {"Repository": REPO_FIELDS, "Issue": ISSUE_FIELDS, "PullRequest": PR_FIELDS}


def gid(kind, number):
    return base64.b64encode(("04:%s%d" % (kind, number)).encode()).decode()


def ungid(value):
    try:
        text = base64.b64decode(value.encode()).decode()
    except Exception:
        return None, None
    body = text[3:] if text.startswith("04:") else text
    kind = body.rstrip("0123456789")
    digits = body[len(kind):]
    if not kind or not digits:
        return (None, None)
    return (kind, int(digits))


def repo_by_gid(value):
    kind, number = ungid(value)
    if kind != "Repository":
        return None
    for repo in list(REGISTRY.values()):
        if repo.seq == number:
            return repo
    return None


def state_upper(value):
    return (value or "open").upper()


def gql_repo(repo):
    return {"__type": "Repository", "repo": repo}


def gql_issue(repo, issue):
    return {"__type": "Issue", "repo": repo, "issue": issue}


def gql_pull(repo, pull):
    return {"__type": "PullRequest", "repo": repo, "pull": pull}


def connection(nodes, has_next=False, end_cursor=None, total=None):
    # totalCount is the size of the whole connection; only the window was passed
    # in before, so a paged read reported its page size as the total.
    return {"__type": "Connection", "nodes": nodes,
            "pageInfo": {"__type": "PageInfo", "hasNextPage": has_next,
                         "hasPreviousPage": False, "endCursor": end_cursor,
                         "startCursor": end_cursor},
            "totalCount": len(nodes) if total is None else total}


def connection_slice(items, args, wrap):
    first = args.get("first")
    after = args.get("after")
    start = 0
    if isinstance(after, str) and after:
        try:
            start = int(base64.b64decode(after.encode()).decode().split(":")[-1])
        except Exception:
            start = 0
    if not isinstance(first, int) or first < 0:
        first = len(items)
    window = items[start : start + first]
    has_next = start + first < len(items)
    cursor = base64.b64encode(("cursor:%d" % (start + len(window))).encode()).decode() if window else None
    return connection([wrap(item) for item in window], has_next, cursor, len(items))


def resolve_field(type_name, obj, name, args, ctx):
    if type_name == "Query":
        return resolve_query_field(name, args, ctx)
    if type_name == "Mutation":
        return resolve_mutation_field(name, args, ctx)
    if type_name == "Repository":
        return resolve_repository_field(obj["repo"], name, args, ctx)
    if type_name == "Issue":
        return resolve_issue_field(obj["repo"], obj["issue"], name, args, ctx)
    if type_name == "PullRequest":
        return resolve_pull_field(obj["repo"], obj["pull"], name, args, ctx)
    if type_name in PAYLOAD_FIELDS:
        field, nested = PAYLOAD_FIELDS[type_name]
        if name == field:
            return (obj.get(field), nested)
        return (None, None)
    if type_name == "User":
        if name != "login":
            raise GQLError('Cannot query field "%s" on type "User".' % name)
        return (obj.get("login"), None)
    if type_name == "Ref":
        return (obj.get("name"), None)
    if type_name == "Connection":
        if name == "nodes":
            return (obj["nodes"], None)
        if name == "totalCount":
            return (obj["totalCount"], None)
        if name == "pageInfo":
            return (obj["pageInfo"], "PageInfo")
        return (None, None)
    if type_name == "PageInfo":
        return (obj.get(name), None)
    if type_name == "CommentEdge":
        return (obj.get("node"), "Comment")
    if type_name == "Comment":
        return (obj.get(name), None)
    if type_name == "Label":
        return (obj.get(name), None)
    if type_name == "File":
        return (obj.get(name), None)
    if type_name == "__Type":
        if name == "name":
            return (obj.get("name"), None)
        if name == "fields":
            listed = obj.get("fields") or []
            return ([{"__type": "__Field", "name": field} for field in listed], None)
        if name == "enumValues":
            listed = obj.get("enumValues") or []
            return ([{"__type": "__EnumValue", "name": value} for value in listed], None)
        return (None, None)
    if type_name in ("__Field", "__EnumValue"):
        return (obj.get(name), None)
    return (None, None)


def resolve_query_field(name, args, ctx):
    if name == "viewer":
        return ({"__type": "User", "login": VIEWER_LOGIN}, "User")
    if name == "repository":
        repo = get_repo(args.get("owner") or "", args.get("name") or "")
        if repo is None or repo.owner.lower() != VIEWER_LOGIN:
            return (None, "Repository")
        return (gql_repo(repo), "Repository")
    if name == "node":
        kind, number = ungid(args.get("id") or "")
        if kind == "Repository":
            repo = repo_by_gid(args.get("id"))
            if repo is not None:
                return (gql_repo(repo), "Repository")
        return (None, None)
    if name == "__type":
        target = args.get("name")
        if target == "SearchType":
            return ({"__type": "__Type", "name": target, "fields": [], "enumValues": SEARCH_ENUMS}, "__Type")
        if target in TYPE_FIELDS:
            return ({"__type": "__Type", "name": target, "fields": TYPE_FIELDS[target]}, "__Type")
        return (None, "__Type")
    raise GQLError('Cannot query field "%s" on type "Query".' % name)


def resolve_repository_field(repo, name, args, ctx):
    if name == "id":
        return (gid("Repository", repo.seq), None)
    if name == "databaseId":
        return (repo.seq, None)
    if name == "name":
        return (repo.name, None)
    if name == "nameWithOwner":
        return (repo.full_name, None)
    if name == "owner":
        return ({"__type": "User", "login": repo.owner}, "User")
    if name == "description":
        return (repo.description, None)
    if name == "isPrivate":
        return (repo.private, None)
    if name == "defaultBranchRef":
        if repo.tip() is None:
            return (None, "Ref")
        return ({"__type": "Ref", "name": repo.default_branch}, "Ref")
    if name == "viewerPermission":
        return ("ADMIN", None)
    if name == "hasIssuesEnabled":
        return (True, None)
    if name == "hasPullRequestsEnabled":
        return (True, None)
    if name == "isEmpty":
        return (not repo.refs, None)
    if name == "isFork":
        return (bool(repo.fork), None)
    if name == "url":
        return ("%s/%s" % (ctx["base_url"], repo.full_name), None)
    if name in ("createdAt", "updatedAt"):
        return (iso(BASE_EPOCH + repo.seq * 60), None)
    if name == "issues":
        wanted = args.get("states")
        states = [state_upper(item) for item in wanted] if isinstance(wanted, list) else None
        items = [issue for issue in repo.issues
                 if states is None or state_upper(issue["state"]) in states]
        order = args.get("orderBy")
        if isinstance(order, dict):
            field = str(order.get("field") or "CREATED_AT").upper()
            descending = str(order.get("direction") or "DESC").upper() != "ASC"
            key = "updated" if field == "UPDATED_AT" else "created"
            # Ties break by number DESC in both directions, so a stable sort on
            # the number first and the chosen field second gives the same answer
            # for either direction.
            items.sort(key=lambda issue: issue["number"], reverse=True)
            items.sort(key=lambda issue: issue.get(key) or 0, reverse=descending)
        else:
            items = list(reversed(items))
        return (connection_slice(items, args, lambda issue: gql_issue(repo, issue)), "Connection")
    if name == "issue":
        issue = find_issue(repo, args.get("number"))
        if issue is None:
            return (None, "Issue")
        return (gql_issue(repo, issue), "Issue")
    if name == "pullRequests":
        return (connection_slice(list(reversed(repo.pulls)), args,
                                 lambda pull: gql_pull(repo, pull)), "Connection")
    if name == "pullRequest":
        pull = find_pull(repo, args.get("number"))
        if pull is None:
            return (None, "PullRequest")
        return (gql_pull(repo, pull), "PullRequest")
    raise GQLError('Cannot query field "%s" on type "Repository".' % name)


def resolve_issue_field(repo, issue, name, args, ctx):
    if name == "id":
        return (gid("Issue", issue["id"]), None)
    if name == "number":
        return (issue["number"], None)
    if name == "title":
        return (issue["title"], None)
    if name == "body":
        return (issue["body"], None)
    if name == "bodyText":
        return (issue["body"], None)
    if name == "state":
        return (state_upper(issue["state"]), None)
    if name == "stateReason":
        return (None, None)
    if name == "author":
        return ({"__type": "User", "login": issue["user"]}, "User")
    if name == "repository":
        return (gql_repo(repo), "Repository")
    if name == "labels":
        return (connection([{"__type": "Label", "name": label["name"]} for label in issue["labels"]]), "Connection")
    if name in ("assignees", "comments"):
        return (connection([]), "Connection")
    if name == "url":
        return ("%s/%s/issues/%d" % (ctx["base_url"], repo.full_name, issue["number"]), None)
    if name == "createdAt":
        return (iso(issue["created"]), None)
    if name == "updatedAt":
        return (iso(issue["updated"]), None)
    if name == "closedAt":
        return (iso(issue["updated"]) if issue["state"] == "closed" else None, None)
    if name == "locked":
        return (False, None)
    raise GQLError('Cannot query field "%s" on type "Issue".' % name)


def resolve_pull_field(repo, pull, name, args, ctx):
    if name == "id":
        return (gid("PullRequest", pull["id"]), None)
    if name == "number":
        return (pull["number"], None)
    if name == "title":
        return (pull["title"], None)
    if name == "body":
        return (pull["body"], None)
    if name == "state":
        if pull["merged"]:
            return ("MERGED", None)
        return (state_upper(pull["state"]), None)
    if name == "headRefName":
        return (pull["head"], None)
    if name == "baseRefName":
        return (pull["base"], None)
    if name == "mergeable":
        return (("MERGEABLE" if pull["mergeable"] else "CONFLICTING"), None)
    if name == "merged":
        return (bool(pull["merged"]), None)
    if name == "mergedAt":
        return (iso(pull["merged_epoch"]) if pull["merged_epoch"] else None, None)
    if name == "isDraft":
        return (False, None)
    if name == "author":
        return ({"__type": "User", "login": pull["user"]}, "User")
    if name == "repository":
        return (gql_repo(repo), "Repository")
    if name == "files":
        changes = pull_files(repo, pull)
        return (connection_slice([{"__type": "File", "path": change["filename"]}
                                  for change in changes], args, lambda entry: entry), "Connection")
    if name == "headRefOid":
        return (repo.refs.get("refs/heads/" + pull["head"]), None)
    if name == "baseRefOid":
        return (repo.refs.get("refs/heads/" + pull["base"]), None)
    if name in ("createdAt", "updatedAt"):
        return (iso(pull["created"]), None)
    if name == "url":
        return ("%s/%s/pull/%d" % (ctx["base_url"], repo.full_name, pull["number"]), None)
    raise GQLError('Cannot query field "%s" on type "PullRequest".' % name)


def resolve_mutation_field(name, args, ctx):
    data = args.get("input") or {}
    if not isinstance(data, dict):
        raise GQLError("input must be an object")
    if name == "createIssue":
        repo = repo_by_gid(data.get("repositoryId"))
        if repo is None:
            raise GQLError("Could not resolve to a Repository with the global id")
        _status, payload, _headers = create_issue(repo, {"title": data.get("title"),
                                                         "body": data.get("body")})
        issue = find_issue(repo, payload["number"])
        return ({"__type": "CreateIssuePayload",
                 "issue": gql_issue(repo, issue)}, "CreateIssuePayload")
    if name == "closeIssue":
        kind, number = ungid(data.get("issueId") or "")
        for repo in list(REGISTRY.values()):
            for issue in repo.issues:
                if gid("Issue", issue["id"]) == data.get("issueId"):
                    issue["state"] = "closed"
                    return ({"__type": "CloseIssuePayload", "issue": gql_issue(repo, issue)},
                            "CloseIssuePayload")
        raise GQLError("Could not resolve to an Issue with the global id")
    if name == "createPullRequest":
        repo = repo_by_gid(data.get("repositoryId"))
        if repo is None:
            raise GQLError("Could not resolve to a Repository with the global id")
        _status, payload, _headers = create_pull(repo, {"title": data.get("title") or "",
                                                        "body": data.get("body") or "",
                                                        "head": data.get("headRefName"),
                                                        "base": data.get("baseRefName")})
        pull = find_pull(repo, payload["number"])
        return ({"__type": "CreatePullRequestPayload",
                 "pullRequest": gql_pull(repo, pull)}, "CreatePullRequestPayload")
    if name == "mergePullRequest":
        for repo in list(REGISTRY.values()):
            for pull in repo.pulls:
                if gid("PullRequest", pull["id"]) == data.get("pullRequestId"):
                    merge_pull(repo, pull, {})
                    return ({"__type": "MergePullRequestPayload",
                             "pullRequest": gql_pull(repo, pull)}, "MergePullRequestPayload")
        raise GQLError("Could not resolve to a PullRequest with the global id")
    if name == "addComment":
        for repo in list(REGISTRY.values()):
            for issue in repo.issues:
                if gid("Issue", issue["id"]) == data.get("subjectId"):
                    number = repo.counters["comment"] + 1
                    repo.counters["comment"] = number
                    comment = {"id": 300000 + number, "body": data.get("body") or "",
                               "user": VIEWER_LOGIN, "created": next_epoch()}
                    repo.comments.setdefault(issue["number"], []).append(comment)
                    node = {"__type": "Comment", "id": gid("IssueComment", comment["id"]),
                            "body": comment["body"]}
                    return ({"__type": "AddCommentPayload",
                             "commentEdge": {"__type": "CommentEdge", "node": node}},
                            "AddCommentPayload")
        raise GQLError("Could not resolve to a node with the global id")
    raise GQLError('Cannot query field "%s" on type "Mutation".' % name)


PAYLOAD_FIELDS = {
    "CreateIssuePayload": ("issue", "Issue"),
    "CloseIssuePayload": ("issue", "Issue"),
    "CreatePullRequestPayload": ("pullRequest", "PullRequest"),
    "MergePullRequestPayload": ("pullRequest", "PullRequest"),
    "AddCommentPayload": ("commentEdge", "CommentEdge"),
}


def complete(type_name, value, selections, ctx):
    if value is None:
        return None
    selections = expand(selections, ctx["vars"], ctx.get("fragments"))
    if not selections:
        return value
    if isinstance(value, list):
        return [complete(type_name, item, selections, ctx) for item in value]
    if isinstance(value, dict) and "nodes" in value and type_name in (None, "Connection"):
        pass
    out = {}
    if isinstance(value, dict) and value.get("__type") == "Connection" and type_name != "Connection":
        type_name = "Connection"
    for selection in selections:
        if selection["kind"] == "inline":
            if value.get("__type") == selection["type"]:
                inner = complete(value.get("__type"), value, selection["selections"], ctx)
                if isinstance(inner, dict):
                    out.update(inner)
            continue
        field = selection["name"]
        key = selection["alias"] or field
        if field == "__typename":
            out[key] = value.get("__type")
            continue
        args = {name: gql_value(node, ctx["vars"]) for name, node in selection["args"].items()}
        try:
            resolved, nested = resolve_field(type_name, value, field, args, ctx)
        except GQLError as err:
            ctx["errors"].append({"message": str(err),
                                  "path": ctx["path"] + [key],
                                  "type": "INVALID_FIELD"})
            out[key] = None
            continue
        if resolved is None:
            out[key] = None
            continue
        target = nested
        if isinstance(resolved, dict):
            target = resolved.get("__type") or nested
        elif isinstance(resolved, list) and resolved and isinstance(resolved[0], dict):
            target = resolved[0].get("__type") or nested
        elif nested is None and isinstance(resolved, dict):
            target = resolved.get("__type")
        ctx["path"].append(key)
        out[key] = complete(target, resolved, selection["selections"], ctx)
        ctx["path"].pop()
    return out


def run_operation(operation, variables, base_url):
    ctx = {"vars": variables, "errors": [], "path": [], "base_url": base_url,
           "fragments": operation.get("fragments") or {}}
    root = "Mutation" if operation["type"] == "mutation" else "Query"
    data = {}
    for selection in expand(operation["selections"], variables, ctx["fragments"]):
        if selection["kind"] == "inline":
            continue
        field = selection["name"]
        key = selection["alias"] or field
        args = {name: gql_value(node, variables) for name, node in selection["args"].items()}
        try:
            resolved, nested = resolve_field(root, {"__type": root}, field, args, ctx)
        except GQLError as err:
            ctx["errors"].append({"message": str(err), "path": [key], "type": "INVALID_FIELD"})
            data[key] = None
            continue
        if resolved is None:
            data[key] = None
            continue
        target = nested
        if isinstance(resolved, dict):
            target = resolved.get("__type") or nested
        ctx["path"].append(key)
        data[key] = complete(target, resolved, selection["selections"], ctx)
        ctx["path"].pop()
    payload = {"data": data}
    if ctx["errors"]:
        payload["errors"] = ctx["errors"]
    return payload


def gql_one(request, base_url):
    if not isinstance(request, dict):
        return {"errors": [{"message": "the request body must be an object"}]}
    query = request.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"errors": [{"message": "a query document is required"}]}
    variables = request.get("variables")
    if not isinstance(variables, dict):
        variables = {}
    operation_name = request.get("operationName")
    try:
        operations = GQLParser(query).parse_document()
    except GQLError as err:
        return {"errors": [{"message": "syntax error: %s" % err}]}
    if not operations:
        return {"errors": [{"message": "the document holds no operation"}]}
    chosen = operations[0]
    if isinstance(operation_name, str) and operation_name:
        for candidate in operations:
            if candidate["name"] == operation_name:
                chosen = candidate
                break
    merged = dict(variables)
    for name, definition in chosen["variables"].items():
        if merged.get(name) is None and definition["default"] is not None:
            merged[name] = gql_value(definition["default"], variables)
    return run_operation(chosen, merged, base_url)


def graphql_dispatch(body, base_url=""):
    if isinstance(body, list):
        return 200, [gql_one(item, base_url) for item in body]
    return 200, gql_one(body, base_url)
