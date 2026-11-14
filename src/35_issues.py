
# --------------------------------------------------------------------------- issues

def issues_rest(repo, method, tail, query, body):
    if not tail:
        if method == "POST":
            return create_issue(repo, body)
        if method == "GET":
            state = (query.get("state") or "open").lower()
            items = list(repo.issues)
            if state != "all":
                items = [i for i in items if i["state"] == state]
            sort = (query.get("sort") or "created").lower()
            direction = (query.get("direction") or "desc").lower()
            if sort == "created":
                items.sort(key=lambda i: i["number"], reverse=(direction == "desc"))
            elif sort == "updated":
                items.sort(key=lambda i: i["updated"], reverse=(direction == "desc"))
            elif sort == "comments":
                items.sort(key=lambda i: len(repo.comments.get(i["number"], [])),
                           reverse=(direction == "desc"))
            rendered = [issue_json(repo, i) for i in items]
            chunk, headers = paginate(rendered, query, "", "")
            headers.pop("Link", None)
            try:
                per_page = max(1, min(int(query.get("per_page", 30)), 100))
            except (TypeError, ValueError):
                per_page = 30
            try:
                page = max(1, int(query.get("page", 1)))
            except (TypeError, ValueError):
                page = 1
            start = (page - 1) * per_page
            return 200, rendered[start : start + per_page], headers
    else:
        try:
            number = int(tail[0])
        except (TypeError, ValueError):
            raise HttpError(404, "Not Found")
        issue = find_issue(repo, number)
        if len(tail) == 1:
            if issue is None:
                raise HttpError(404, "Not Found")
            if method == "GET":
                return 200, issue_json(repo, issue), {}
            if method == "PATCH":
                if not isinstance(body, dict):
                    raise HttpError(400, "Body should be a JSON object")
                if "state" in body and body["state"] in ("open", "closed"):
                    issue["state"] = body["state"]
                if "title" in body and isinstance(body["title"], str):
                    issue["title"] = body["title"]
                if "body" in body and isinstance(body["body"], str):
                    issue["body"] = body["body"]
                if "labels" in body and isinstance(body["labels"], list):
                    resolved = resolve_labels(repo, body["labels"])
                    issue["labels"] = resolved
                issue["updated"] = next_epoch()
                return 200, issue_json(repo, issue), {}
        if len(tail) == 2 and tail[1] == "comments":
            if issue is None:
                raise HttpError(404, "Not Found")
            if method == "POST":
                if not isinstance(body, dict):
                    raise HttpError(400, "Body should be a JSON object")
                text = body.get("body")
                if not isinstance(text, str):
                    raise HttpError(422, "body is required")
                cid = repo.bump("comment")
                comment = {"id": cid, "body": text, "created": next_epoch()}
                repo.comments.setdefault(number, []).append(comment)
                return 201, comment_json(repo, number, comment), {}
            if method == "GET":
                items = repo.comments.get(number, [])
                return 200, [comment_json(repo, number, c) for c in items], {}
    raise HttpError(404, "Not Found")


def comment_json(repo, number, comment):
    return {
        "id": comment["id"],
        "node_id": "MDEyOklzc3VlQ29tbWVudA==" + str(comment["id"]),
        "body": comment["body"],
        "user": user_json(),
        "created_at": iso(comment["created"]),
        "updated_at": iso(comment["created"]),
        "issue_url": "https://api.github.com/repos/%s/issues/%d" % (repo.full_name, number),
        "html_url": "",
        "url": "",
    }


def resolve_labels(repo, names):
    out = []
    for name in names:
        label = repo.labels.get(name) if isinstance(name, str) else None
        if label is None:
            raise HttpError(422, "Validation Failed: label %r does not exist" % (name,))
        out.append(label)
    return out


def create_issue(repo, body):
    if not isinstance(body, dict):
        raise HttpError(400, "Body should be a JSON object")
    title = body.get("title")
    if not isinstance(title, str) or title == "":
        raise HttpError(422, "Validation Failed: title is required")
    labels = resolve_labels(repo, body.get("labels") or []) if body.get("labels") else []
    number = repo.bump("issue")
    epoch = next_epoch()
    issue = {
        "id": 100000 + number,
        "number": number,
        "title": title,
        "body": body.get("body") or "",
        "state": "open",
        "labels": labels,
        "created": epoch,
        "updated": epoch,
        "user": VIEWER_LOGIN,
    }
    repo.issues.append(issue)
    return 201, issue_json(repo, issue), {}


# --------------------------------------------------------------------------- pull requests

def pulls_rest(repo, method, tail, query, body):
    if not tail:
        if method == "POST":
            return create_pull(repo, body)
        if method == "GET":
            state = (query.get("state") or "open").lower()
            items = list(repo.pulls)
            if state != "all":
                items = [p for p in items if p["state"] == state]
            items.sort(key=lambda p: p["number"], reverse=True)
            try:
                per_page = max(1, min(int(query.get("per_page", 30)), 100))
            except (TypeError, ValueError):
                per_page = 30
            try:
                page = max(1, int(query.get("page", 1)))
            except (TypeError, ValueError):
                page = 1
            start = (page - 1) * per_page
            return 200, [pull_json(repo, p) for p in items[start : start + per_page]], {}
    try:
        number = int(tail[0])
    except (TypeError, ValueError):
        raise HttpError(404, "Not Found")
    pull = find_pull(repo, number)
    if len(tail) == 1:
        if pull is None:
            raise HttpError(404, "Not Found")
        if method == "GET":
            return 200, pull_json(repo, pull), {}
        if method == "PATCH":
            if isinstance(body, dict) and body.get("state") in ("open", "closed"):
                pull["state"] = body["state"]
                pull["updated"] = next_epoch()
            return 200, pull_json(repo, pull), {}
    if len(tail) == 2 and tail[1] == "files":
        if pull is None:
            raise HttpError(404, "Not Found")
        if method == "GET":
            return 200, pull_files(repo, pull), {}
    if len(tail) == 2 and tail[1] == "merge":
        if pull is None:
            raise HttpError(404, "Not Found")
        if method == "PUT":
            return merge_pull(repo, pull, body)
        if method == "GET":
            return 204, None, {}
    raise HttpError(404, "Not Found")


def branch_of(name):
    name = name.split(":")[-1]
    return name


def create_pull(repo, body):
    if not isinstance(body, dict):
        raise HttpError(400, "Body should be a JSON object")
    head = branch_of(body.get("head") or "")
    base = branch_of(body.get("base") or repo.default_branch)
    head_sha = repo.refs.get("refs/heads/" + head)
    base_sha = repo.refs.get("refs/heads/" + base)
    if head_sha is None:
        raise HttpError(422, "Validation Failed: head branch does not exist")
    if base_sha is None:
        raise HttpError(422, "Validation Failed: base branch does not exist")
    number = repo.bump("pull")
    epoch = next_epoch()
    pull = {
        "id": 200000 + number,
        "number": number,
        "state": "open",
        "title": body.get("title") or "",
        "body": body.get("body") or "",
        "head": head,
        "base": base,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "merged": False,
        "merged_epoch": None,
        "merge_sha": None,
        "mergeable": True,
        "created": epoch,
        "updated": epoch,
        "user": VIEWER_LOGIN,
    }
    pull["mergeable"] = merge_conflicts(repo, pull) == []
    repo.pulls.append(pull)
    return 201, pull_json(repo, pull), {}


def merge_conflicts(repo, pull):
    """Paths the pull changes whose base copy also moved on the base branch since."""
    base_sha = pull["base_sha"]
    head_tip = repo.refs.get("refs/heads/" + pull["head"])
    base_tip = repo.refs.get("refs/heads/" + pull["base"])
    if head_tip is None or base_tip is None:
        return ["<missing branch>"]
    mine = diff_trees(base_sha, head_tip)
    theirs = diff_trees(base_sha, base_tip)
    out = []
    for path, (old, new) in mine.items():
        other = theirs.get(path)
        if other is None:
            continue
        if (other[1] or (None, None))[1] != (new or (None, None))[1]:
            out.append(path)
    return out


def pull_files(repo, pull):
    head_tip = repo.refs.get("refs/heads/" + pull["head"])
    if head_tip is None:
        return []
    changes = diff_trees(pull["base_sha"], head_tip)
    out = []
    for path in sorted(changes):
        old, new = changes[path]
        if old is None:
            status = "added"
        elif new is None:
            status = "removed"
        else:
            status = "modified"
        before = blob_bytes(old)
        after = blob_bytes(new)
        additions, deletions = line_counts(before, after)
        out.append({
            "sha": (new or old)[1],
            "filename": path,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
            "blob_url": "",
            "raw_url": "",
            "contents_url": "",
            "patch": "",
        })
    return out


def line_counts(before, after):
    old_lines = before.decode("utf-8", "replace").splitlines()
    new_lines = after.decode("utf-8", "replace").splitlines()
    if not old_lines:
        return (len(new_lines), 0)
    if not new_lines:
        return (0, len(old_lines))
    import difflib
    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if tag == "replace":
            additions += j2 - j1
            deletions += i2 - i1
        elif tag == "insert":
            additions += j2 - j1
        elif tag == "delete":
            deletions += i2 - i1
    return (additions, deletions)


def merge_pull(repo, pull, body):
    if pull["merged"]:
        return 200, {"sha": pull["merge_sha"], "merged": True,
                     "message": "Pull Request already merged"}, {}
    conflicts = merge_conflicts(repo, pull)
    if conflicts:
        raise HttpError(405, "Pull Request is not mergeable")
    head_tip = repo.refs.get("refs/heads/" + pull["head"])
    base_tip = repo.refs.get("refs/heads/" + pull["base"])
    method = (body or {}).get("merge_method") or "merge"
    tree = commit_tree(head_tip)
    message = (body or {}).get("commit_message") or ("%s (#%d)" % (pull["title"], pull["number"]))
    payload = commit_payload(tree, [base_tip], message)
    merge_sha = STORE.put("commit", payload)
    repo.refs["refs/heads/" + pull["base"]] = merge_sha
    pull["merged"] = True
    pull["state"] = "closed"
    pull["merge_sha"] = merge_sha
    pull["merged_epoch"] = next_epoch()
    pull["updated"] = pull["merged_epoch"]
    pull["mergeable"] = True
    return 200, {"sha": merge_sha, "merged": True,
                 "message": "Pull Request successfully merged"}, {}
