
# --------------------------------------------------------------------------- REST v3

def need_repo(owner, name):
    repo = get_repo(owner, name)
    if repo is None or repo.owner.lower() != VIEWER_LOGIN:
        raise HttpError(404, "Not Found")
    return repo


def issue_json(repo, issue):
    out = {
        "id": issue["id"],
        "node_id": "MDU6SXNzdWU=" + str(issue["id"]),
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": [{"id": i + 1, "node_id": "MDU6TGFiZWw=" + str(i + 1), "name": lb["name"],
                    "color": lb["color"], "default": False} for i, lb in enumerate(issue["labels"])],
        "user": user_json(issue.get("user")),
        "assignee": None,
        "assignees": [],
        "milestone": None,
        "locked": False,
        "comments": len(repo.comments.get(issue["number"], [])),
        "created_at": iso(issue["created"]),
        "updated_at": iso(issue["updated"]),
        "closed_at": iso(issue["updated"]) if issue["state"] == "closed" else None,
        "html_url": "https://github.com/%s/issues/%d" % (repo.full_name, issue["number"]),
        "url": "https://api.github.com/repos/%s/issues/%d" % (repo.full_name, issue["number"]),
        "repository_url": "https://api.github.com/repos/" + repo.full_name,
    }
    return out


def pull_json(repo, pull):
    return {
        "id": pull["id"],
        "node_id": "MDExOlB1bGxSZXF1ZXN0" + str(pull["id"]),
        "number": pull["number"],
        "state": pull["state"],
        "title": pull["title"],
        "body": pull["body"],
        "user": user_json(pull.get("user")),
        "head": {"ref": pull["head"], "sha": pull["head_sha"], "label": repo.owner + ":" + pull["head"],
                 "user": user_json(repo.owner), "repo": None},
        "base": {"ref": pull["base"], "sha": pull["base_sha"], "label": repo.owner + ":" + pull["base"],
                 "user": user_json(repo.owner), "repo": None},
        "merged": pull["merged"],
        "merged_at": iso(pull["merged_epoch"]) if pull["merged"] else None,
        "merge_commit_sha": pull["merge_sha"],
        "mergeable": pull["mergeable"],
        "mergeable_state": "clean" if pull["mergeable"] else "dirty",
        "draft": False,
        "created_at": iso(pull["created"]),
        "updated_at": iso(pull["updated"]),
        "closed_at": iso(pull["updated"]) if pull["state"] == "closed" else None,
        "html_url": "https://github.com/%s/pull/%d" % (repo.full_name, pull["number"]),
        "url": "https://api.github.com/repos/%s/pulls/%d" % (repo.full_name, pull["number"]),
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
    }


def find_issue(repo, number):
    for issue in repo.issues:
        if issue["number"] == number:
            return issue
    return None


def find_pull(repo, number):
    for pull in repo.pulls:
        if pull["number"] == number:
            return pull
    return None


def paginate(items, query, base_url, path):
    try:
        per_page = int(query.get("per_page", 30))
    except (TypeError, ValueError):
        per_page = 30
    per_page = max(1, min(per_page, 100))
    try:
        page = int(query.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    start = (page - 1) * per_page
    chunk = items[start : start + per_page]
    headers = {}
    links = []
    last = max(1, (len(items) + per_page - 1) // per_page)
    if page < last:
        links.append('<%s%s?per_page=%d&page=%d>; rel="next"' % (base_url, path, per_page, page + 1))
        links.append('<%s%s?per_page=%d&page=%d>; rel="last"' % (base_url, path, per_page, last))
    if page > 1:
        links.append('<%s%s?per_page=%d&page=%d>; rel="prev"' % (base_url, path, per_page, page - 1))
        links.append('<%s%s?per_page=%d&page=1>; rel="first"' % (base_url, path, per_page))
    if links:
        headers["Link"] = ", ".join(links)
    return chunk, headers


def rest_dispatch(method, parts, query, body, base_url):
    """Returns (status, payload, headers). Raises HttpError."""
    head = parts[0] if parts else ""

    if head == "":
        if method != "GET":
            raise HttpError(404, "Not Found")
        return 200, {
            "current_user_url": base_url + "/api/v3/user",
            "repository_url": base_url + "/api/v3/repos/{owner}/{repo}",
            "user_repositories_url": base_url + "/api/v3/user/repos",
            "rate_limit_url": base_url + "/api/v3/rate_limit",
            "emojis_url": base_url + "/api/v3/emojis",
        }, {"X-OAuth-Scopes": "repo, read:org"}

    if head == "meta" and method == "GET":
        return 200, {"installed_version": "3.0.0-forge", "verifiable_password_authentication": False}, {}

    if head == "user":
        if len(parts) == 1 and method == "GET":
            return 200, user_json(), {}
        if len(parts) == 2 and parts[1] == "repos":
            if method == "POST":
                return create_repo(body)
            if method == "GET":
                items = [repo_json(r) for r in viewer_repos()]
                chunk, headers = paginate(items, query, base_url, "/api/v3/user/repos")
                return 200, chunk, headers

    if head == "repos" and len(parts) >= 3:
        owner, name = parts[1], parts[2]
        rest = parts[3:]
        repo = need_repo(owner, name)

        if not rest:
            if method == "GET":
                return 200, repo_json(repo), {}
            if method == "PATCH":
                if isinstance(body, dict) and "description" in body:
                    repo.description = body["description"] or ""
                if isinstance(body, dict) and "private" in body:
                    repo.private = bool(body["private"])
                return 200, repo_json(repo), {}
            if method == "DELETE":
                drop_repo(repo.owner, repo.name)
                return 204, None, {}

        section = rest[0]
        tail = rest[1:]

        if section == "branches":
            if not tail and method == "GET":
                return 200, branches_json(repo), {}
            if len(tail) == 1 and method == "GET":
                sha = repo.refs.get("refs/heads/" + tail[0])
                if sha is None:
                    raise HttpError(404, "Branch not found")
                return 200, {"name": tail[0], "commit": {"sha": sha, "url": ""},
                             "protected": False}, {}

        if section == "commits":
            if method == "GET":
                return 200, commits_json(repo, query), {}
            if method == "POST":
                return create_commit(repo, body)

        if section == "git":
            return git_rest(repo, method, tail, body)

        if section == "contents":
            return contents_rest(repo, method, tail, query, body)

        if section == "labels":
            if method == "POST":
                return create_label(repo, body)
            if method == "GET":
                return 200, [{"id": i + 1, "name": lb["name"], "color": lb["color"],
                              "default": False} for i, lb in enumerate(repo.labels.values())], {}

        if section == "issues":
            return issues_rest(repo, method, tail, query, body)

        if section == "pulls":
            return pulls_rest(repo, method, tail, query, body)

    raise HttpError(404, "Not Found")


def create_repo(body):
    if not isinstance(body, dict):
        raise HttpError(400, "Body should be a JSON object")
    name = body.get("name")
    if not isinstance(name, str) or name.strip() == "":
        raise HttpError(422, "Repository name is required")
    name = name.strip()
    if "/" in name or name in (".", ".."):
        raise HttpError(422, "Repository name is invalid")
    existing = get_repo(VIEWER_LOGIN, name)
    if existing is not None:
        raise HttpError(422, "Repository creation failed. name already exists on this account")
    repo = get_repo(VIEWER_LOGIN, name, create=True, private=bool(body.get("private", False)),
                    description=body.get("description") or "")
    if isinstance(body.get("default_branch"), str) and body["default_branch"]:
        repo.default_branch = body["default_branch"]
    return 201, repo_json(repo), {"Location": "/api/v3/repos/" + repo.full_name}


def branches_json(repo):
    out = []
    for ref, sha in sorted(repo.refs.items()):
        if not ref.startswith("refs/heads/"):
            continue
        out.append({"name": ref[len("refs/heads/"):], "commit": {"sha": sha, "url": ""},
                    "protected": False})
    return out


def commits_json(repo, query):
    ref = query.get("sha") or repo.default_branch
    tip = resolve_commit(repo, ref)
    if tip is None:
        return []
    out = []
    for sha in ancestors(repo, tip):
        try:
            tree, parents, message, author, committer = parse_commit(STORE.get(sha)[1])
        except KeyError:
            continue
        name, email, epoch, tz = parse_signature(author)
        out.append({
            "sha": sha,
            "node_id": "MDY6Q29tbWl0" + sha,
            "commit": {
                "author": {"name": name, "email": email, "date": iso(epoch)},
                "committer": {"name": name, "email": email, "date": iso(epoch)},
                "message": message.rstrip("\n"),
                "tree": {"sha": tree},
                "comment_count": 0,
            },
            "parents": [{"sha": p} for p in parents],
            "url": "https://api.github.com/repos/%s/commits/%s" % (repo.full_name, sha),
        })
    return out


def create_commit(repo, body):
    if not isinstance(body, dict):
        raise HttpError(400, "Body should be a JSON object")
    message = body.get("message") or "commit"
    tree = body.get("tree")
    parents = body.get("parents") or []
    if not isinstance(tree, str) or not STORE.has(tree):
        raise HttpError(422, "Tree not found")
    sha = STORE.put("commit", commit_payload(tree, [p for p in parents if isinstance(p, str)], message))
    return 201, {"sha": sha, "tree": {"sha": tree}, "message": message,
                 "parents": [{"sha": p} for p in parents]}, {}


def create_label(repo, body):
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        raise HttpError(422, "Label name is required")
    name = body["name"]
    color = body.get("color") or "ededed"
    repo.labels[name] = {"name": name, "color": color}
    return 201, {"id": repo.bump("label"), "node_id": "MDU6TGFiZWw=" + str(repo.counters["label"]),
                 "name": name, "color": color, "default": False}, {}


def git_rest(repo, method, tail, body):
    if not tail:
        raise HttpError(404, "Not Found")
    what = tail[0]
    if what == "refs":
        if method == "GET" and len(tail) == 1:
            return 200, [{"ref": ref, "object": {"sha": sha, "type": "commit"}}
                         for ref, sha in sorted(repo.refs.items())], {}
        if method == "POST":
            if not isinstance(body, dict):
                raise HttpError(400, "Body should be a JSON object")
            ref = body.get("ref") or ""
            sha = body.get("sha") or ""
            if not ref.startswith("refs/"):
                raise HttpError(422, "Reference name must start with refs/")
            if not STORE.has(sha):
                raise HttpError(422, "Object does not exist")
            repo.refs[ref] = sha
            return 201, {"ref": ref, "object": {"sha": sha, "type": "commit"}, "url": ""}, {}
        if method == "GET" and len(tail) > 1:
            ref = "/".join(tail[1:])
            sha = repo.refs.get(ref)
            if sha is None:
                raise HttpError(404, "Not Found")
            return 200, {"ref": ref, "object": {"sha": sha, "type": "commit"}, "url": ""}, {}
        if method == "PATCH" and len(tail) > 1:
            ref = "/".join(tail[1:])
            if ref not in repo.refs:
                raise HttpError(404, "Not Found")
            sha = (body or {}).get("sha")
            if not isinstance(sha, str) or not STORE.has(sha):
                raise HttpError(422, "Object does not exist")
            repo.refs[ref] = sha
            return 200, {"ref": ref, "object": {"sha": sha, "type": "commit"}, "url": ""}, {}
    if what == "commits" and method == "GET" and len(tail) == 2:
        sha = tail[1]
        if not STORE.has(sha):
            raise HttpError(404, "Not Found")
        tree, parents, message, author, committer = parse_commit(STORE.get(sha)[1])
        return 200, {"sha": sha, "tree": {"sha": tree}, "message": message,
                     "parents": [{"sha": p} for p in parents]}, {}
    if what == "trees" and method == "GET" and len(tail) == 2:
        sha = tail[1]
        if not STORE.has(sha):
            raise HttpError(404, "Not Found")
        kind, payload = STORE.get(sha)
        tree = []
        if kind == "tree":
            for mode, name, child in parse_tree(payload):
                tree.append({"path": name.decode("utf-8", "replace"),
                             "mode": "040000" if mode == 0o40000 else "100644",
                             "type": "tree" if mode == 0o40000 else "blob", "sha": child})
        return 200, {"sha": sha, "tree": tree, "truncated": False}, {}
    raise HttpError(404, "Not Found")


def contents_rest(repo, method, tail, query, body):
    path = "/".join(tail)
    ref = query.get("ref") or repo.default_branch
    tip = resolve_commit(repo, ref)

    if method == "GET":
        if tip is None:
            raise HttpError(404, "Not Found")
        root = commit_tree(tip)
        if path == "":
            return 200, dir_listing(repo, root, ""), {}
        entry = path_blob(root, path)
        if entry is not None:
            mode, sha = entry
            blob = STORE.get(sha)[1]
            return 200, {
                "type": "file",
                "encoding": "base64",
                "size": len(blob),
                "name": path.split("/")[-1],
                "path": path,
                "content": b64e(blob),
                "sha": sha,
                "url": "https://api.github.com/repos/%s/contents/%s" % (repo.full_name, path),
                "git_url": "", "html_url": "", "download_url": None,
            }, {}
        dirs = tree_dirs(root)
        if path in dirs:
            return 200, dir_listing(repo, dirs[path], path), {}
        raise HttpError(404, "Not Found")

    if method == "PUT":
        if not isinstance(body, dict):
            raise HttpError(400, "Body should be a JSON object")
        branch = body.get("branch") or repo.default_branch
        message = body.get("message") or "commit"
        content = body.get("content")
        if not isinstance(content, str):
            raise HttpError(422, "content is required")
        raw = b64d(content)
        tip = repo.refs.get("refs/heads/" + branch)
        current = None
        if tip is not None:
            current = path_blob(commit_tree(tip), path)
        if body.get("sha") is not None:
            if current is None or current[1] != str(body["sha"]).lower():
                raise HttpError(409, "sha does not match the current file")
        elif current is not None:
            raise HttpError(422, "\"sha\" wasn't supplied.")
        blob_sha = STORE.put("blob", raw)
        commit_sha = commit_on_branch(repo, branch, {path: raw}, message)
        status = 200 if current is not None else 201
        return status, {
            "content": {"name": path.split("/")[-1], "path": path, "sha": blob_sha,
                        "size": len(raw), "encoding": "base64", "content": b64e(raw),
                        "type": "file", "url": ""},
            "commit": {"sha": commit_sha, "message": message,
                       "tree": {"sha": commit_tree(commit_sha)}, "parents": []},
        }, {}

    if method == "DELETE":
        if not isinstance(body, dict):
            raise HttpError(400, "Body should be a JSON object")
        branch = body.get("branch") or repo.default_branch
        tip = repo.refs.get("refs/heads/" + branch)
        if tip is None:
            raise HttpError(404, "Not Found")
        current = path_blob(commit_tree(tip), path)
        if current is None:
            raise HttpError(404, "Not Found")
        if body.get("sha") is not None and str(body["sha"]).lower() != current[1]:
            raise HttpError(409, "sha does not match the current file")
        commit_sha = commit_on_branch(repo, branch, {path: None}, body.get("message") or "delete")
        return 200, {"content": None, "commit": {"sha": commit_sha,
                                                 "message": body.get("message") or "delete",
                                                 "tree": {"sha": commit_tree(commit_sha)},
                                                 "parents": []}}, {}

    raise HttpError(404, "Not Found")


def dir_listing(repo, tree_sha, prefix):
    out = []
    for mode, name, sha in parse_tree(STORE.get(tree_sha)[1]):
        raw = name.decode("utf-8", "replace")
        path = prefix + "/" + raw if prefix else raw
        if mode == 0o40000:
            out.append({"type": "dir", "name": raw, "path": path, "sha": sha, "size": 0,
                        "url": "", "git_url": "", "html_url": "", "download_url": None})
        else:
            out.append({"type": "file", "name": raw, "path": path, "sha": sha,
                        "size": len(STORE.get(sha)[1]) if STORE.has(sha) else 0,
                        "url": "", "git_url": "", "html_url": "", "download_url": None})
    out.sort(key=lambda e: e["name"])
    return out
