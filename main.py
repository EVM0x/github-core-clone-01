"""github-core-clone-01 entry: a self-contained GitHub-compatible forge.

One process, standard library only, no UI.  Serves four surfaces from one store:

  /{owner}/{repo}.git/...   git smart HTTP (upload-pack v2, receive-pack v0)
  /api/v3/...               GitHub REST v3
  /api/graphql              GitHub GraphQL v4
  /mcp                      Model Context Protocol, streamable HTTP

The lifecycle is the starter's: bind an ephemeral port, POST it to the env's
/register, then long-poll /done until the env says the suite is over.
"""

import base64
import hashlib
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- identity

# The suite's bearer token names one login; every repository belongs to it and the
# same name under any other owner is a 404.  The scenario is built from the env's
# one sealed seed, so the login is the same string in every trial and instance.
VIEWER_LOGIN = "adaiversen"
VIEWER_NAME = "ada iversen"
VIEWER_EMAIL = "adaiversen@example.invalid"
STRANGER_LOGIN = "olgabauer"

DEFAULT_BRANCH = "main"
ZERO_OID = "0" * 40

# Commit dates the forge itself stamps.  A fixed epoch keeps every generated commit
# byte-identical across runs; the trial's own now is used when the env provides it.
BASE_EPOCH = 1700000000
_EPOCH_OFFSET = [0]


def next_epoch():
    _EPOCH_OFFSET[0] += 60
    return BASE_EPOCH + _EPOCH_OFFSET[0]


# --------------------------------------------------------------------------- git objects


def object_sha(kind, payload):
    """The SHA-1 of a git object: hash of '<type> <size>\\0<payload>'."""
    header = ("%s %d" % (kind, len(payload))).encode()
    return hashlib.sha1(header + b"\x00" + payload).hexdigest()


class Store:
    """Every git object ever written, one flat dict: sha -> (type, payload)."""

    def __init__(self):
        self.objects = {}
        self.lock = threading.Lock()

    def put(self, kind, payload):
        sha = object_sha(kind, payload)
        with self.lock:
            if sha not in self.objects:
                self.objects[sha] = (kind, payload)
        return sha

    def get(self, sha):
        entry = self.objects.get(sha)
        if entry is None:
            raise KeyError("missing object " + sha)
        return entry

    def has(self, sha):
        return sha in self.objects


STORE = Store()


def encode_tree(entries):
    """entries: list of (mode:int, name:bytes, sha:hex) -> tree payload bytes."""
    out = bytearray()
    for mode, name, sha in sorted(entries, key=lambda e: e[1]):
        out += ("%o" % mode).encode() + b" " + name + b"\x00" + bytes.fromhex(sha)
    return bytes(out)


def parse_tree(payload):
    """tree payload -> list of (mode, name, sha)."""
    out = []
    i = 0
    n = len(payload)
    while i < n:
        sp = payload.index(b" ", i)
        nul = payload.index(b"\x00", sp)
        mode = int(payload[i:sp], 8)
        name = payload[sp + 1 : nul]
        sha = payload[nul + 1 : nul + 21].hex()
        out.append((mode, name, sha))
        i = nul + 21
    return out


def commit_payload(tree_sha, parents, message, author=None, epoch=None):
    who = author or (VIEWER_NAME, VIEWER_EMAIL)
    when = epoch if epoch is not None else next_epoch()
    stamp = "%d +0000" % when
    lines = ["tree " + tree_sha]
    for parent in parents:
        lines.append("parent " + parent)
    lines.append("author %s <%s> %s" % (who[0], who[1], stamp))
    lines.append("committer %s <%s> %s" % (who[0], who[1], stamp))
    body = "\n".join(lines) + "\n\n" + message
    if not body.endswith("\n"):
        body += "\n"
    return body.encode()


def parse_commit(payload):
    """commit payload -> (tree, [parents], message, author_line, committer_line)."""
    text = payload.decode("utf-8", "replace")
    head, _, message = text.partition("\n\n")
    tree = ""
    parents = []
    author = ""
    committer = ""
    for line in head.split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]
    return tree, parents, message, author, committer


def parse_signature(line):
    """'Name <email> 1700000000 +0000' -> (name, email, epoch, tz)."""
    lt = line.rfind("<")
    gt = line.rfind(">")
    if lt < 0 or gt < 0:
        return (line.strip(), "", 0, "+0000")
    name = line[:lt].strip()
    email = line[lt + 1 : gt]
    rest = line[gt + 1 :].strip().split()
    epoch = int(rest[0]) if rest and rest[0].lstrip("-").isdigit() else 0
    tz = rest[1] if len(rest) > 1 else "+0000"
    return (name, email, epoch, tz)


# --------------------------------------------------------------------------- trees as paths


def flatten_tree(tree_sha, prefix="", out=None):
    """tree sha -> {path: (mode, blob_sha)} for every blob under it."""
    if out is None:
        out = {}
    try:
        kind, payload = STORE.get(tree_sha)
    except KeyError:
        return out
    if kind != "tree":
        return out
    for mode, name, sha in parse_tree(payload):
        path = prefix + name.decode("utf-8", "replace")
        if mode == 0o40000:
            flatten_tree(sha, path + "/", out)
        else:
            out[path] = (mode, sha)
    return out


def build_tree_from_paths(files):
    """{path: (mode, blob_sha)} -> root tree sha, writing every subtree."""
    root = {}
    for path, entry in files.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = entry

    def write(node):
        entries = []
        for name, value in node.items():
            raw = name.encode()
            if isinstance(value, dict):
                entries.append((0o40000, raw, write(value)))
            else:
                mode, sha = value
                entries.append((mode, raw, sha))
        return STORE.put("tree", encode_tree(entries))

    return write(root)


def path_blob(tree_sha, path):
    """(mode, sha) for one path inside a tree, or None."""
    return flatten_tree(tree_sha).get(path)


def tree_dirs(tree_sha, prefix="", out=None):
    """{dirpath: tree_sha} for every directory (including '') under a tree."""
    if out is None:
        out = {"": tree_sha}
    try:
        kind, payload = STORE.get(tree_sha)
    except KeyError:
        return out
    if kind != "tree":
        return out
    for mode, name, sha in parse_tree(payload):
        if mode == 0o40000:
            sub = prefix + name.decode("utf-8", "replace")
            out[sub] = sha
            tree_dirs(sha, sub + "/", out)
    return out

# --------------------------------------------------------------------------- repositories

class HttpError(Exception):
    def __init__(self, status, message, headers=None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.headers = headers or {}


class Repo:
    __slots__ = (
        "owner", "name", "private", "description", "default_branch", "refs", "issues",
        "pulls", "labels", "comments", "counters", "seq", "fork",
    )

    def __init__(self, owner, name, private, description, seq):
        self.owner = owner
        self.name = name
        self.private = bool(private)
        self.description = description or ""
        self.default_branch = DEFAULT_BRANCH
        self.refs = {}
        self.issues = []
        self.pulls = []
        self.labels = {}
        self.comments = {}
        self.counters = {"issue": 0, "pull": 0, "comment": 0, "label": 0}
        self.seq = seq
        self.fork = False

    @property
    def full_name(self):
        return self.owner + "/" + self.name

    def tip(self, branch=None):
        return self.refs.get("refs/heads/" + (branch or self.default_branch))

    def bump(self, key):
        self.counters[key] += 1
        return self.counters[key]


REGISTRY = {}
REGISTRY_LOCK = threading.Lock()
SEQ = [0]


def get_repo(owner, name, create=False, private=False, description=""):
    key = (owner.lower(), name.lower())
    with REGISTRY_LOCK:
        repo = REGISTRY.get(key)
        if repo is None:
            if not create:
                return None
            SEQ[0] += 1
            repo = Repo(owner, name, private, description, SEQ[0])
            REGISTRY[key] = repo
        return repo


def drop_repo(owner, name):
    with REGISTRY_LOCK:
        REGISTRY.pop((owner.lower(), name.lower()), None)


def viewer_repos():
    with REGISTRY_LOCK:
        items = [r for r in REGISTRY.values() if r.owner.lower() == VIEWER_LOGIN]
    items.sort(key=lambda r: r.seq)
    return items


# --------------------------------------------------------------------------- git helpers


def resolve_commit(repo, ref):
    """A branch name, ref name or 40-hex id -> commit sha, or None."""
    if ref is None or ref == "":
        ref = repo.default_branch
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()):
        return ref.lower() if STORE.has(ref.lower()) else None
    for candidate in ("refs/heads/" + ref, ref, "refs/tags/" + ref):
        sha = repo.refs.get(candidate)
        if sha:
            return sha
    return None


def commit_tree(sha):
    kind, payload = STORE.get(sha)
    if kind != "commit":
        return None
    return parse_commit(payload)[0]


def ancestors(repo, sha, limit=10000):
    """Commit shas reachable from `sha`, newest first (depth-first, first parent first)."""
    seen = []
    seen_set = set()
    stack = [sha]
    while stack and len(seen) < limit:
        cur = stack.pop(0)
        if cur in seen_set or not STORE.has(cur):
            continue
        seen_set.add(cur)
        seen.append(cur)
        try:
            tree, parents, message, author, committer = parse_commit(STORE.get(cur)[1])
        except (KeyError, ValueError):
            continue
        for parent in parents:
            if parent not in seen_set:
                stack.append(parent)
    return seen


def diff_trees(base_sha, head_sha):
    """{path: (old_entry|None, new_entry|None)} between two commit/tree ids."""
    old = flatten_tree(commit_tree(base_sha)) if base_sha else {}
    new = flatten_tree(commit_tree(head_sha)) if head_sha else {}
    out = {}
    for path in set(old) | set(new):
        a = old.get(path)
        b = new.get(path)
        if a is None or b is None or a[1] != b[1]:
            out[path] = (a, b)
    return out


def blob_bytes(entry):
    if entry is None:
        return b""
    try:
        return STORE.get(entry[1])[1]
    except KeyError:
        return b""


def commit_on_branch(repo, branch, changes, message, author=None, parents_override=None):
    """Write blobs+trees for {path: bytes|None} and a commit on `branch`; returns commit sha."""
    ref = "refs/heads/" + branch
    tip = repo.refs.get(ref)
    if tip is None and parents_override is not None:
        tip = parents_override
    files = flatten_tree(commit_tree(tip)) if tip else {}
    for path, content in changes.items():
        if content is None:
            files.pop(path, None)
        else:
            blob = content.encode() if isinstance(content, str) else content
            files[path] = (0o100644, STORE.put("blob", blob))
    tree = build_tree_from_paths(files)
    payload = commit_payload(tree, [tip] if tip else [], message, author)
    sha = STORE.put("commit", payload)
    repo.refs[ref] = sha
    return sha


# --------------------------------------------------------------------------- json helpers


def b64e(data):
    if isinstance(data, str):
        data = data.encode()
    return base64.b64encode(data).decode()


def b64d(text):
    try:
        return base64.b64decode(text.encode() if isinstance(text, str) else text)
    except Exception:
        return b""


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def user_json(login=None):
    login = login or VIEWER_LOGIN
    name = VIEWER_NAME if login == VIEWER_LOGIN else login
    return {
        "login": login,
        "id": 1000,
        "node_id": "MDQ6VXNlcjEwMDA=",
        "avatar_url": "https://avatars.githubusercontent.com/u/1000?v=4",
        "gravatar_id": "",
        "url": "https://api.github.com/users/" + login,
        "html_url": "https://github.com/" + login,
        "type": "User",
        "site_admin": False,
    }


def repo_json(repo):
    return {
        "id": repo.seq,
        "node_id": "MDEwOlJlcG9zaXRvcnk=" + str(repo.seq),
        "name": repo.name,
        "full_name": repo.full_name,
        "private": repo.private,
        "owner": user_json(repo.owner),
        "html_url": "https://github.com/" + repo.full_name,
        "description": repo.description,
        "fork": False,
        "url": "https://api.github.com/repos/" + repo.full_name,
        "default_branch": repo.default_branch,
        "created_at": iso(BASE_EPOCH),
        "updated_at": iso(BASE_EPOCH),
        "pushed_at": iso(BASE_EPOCH),
        "has_issues": True,
        "has_wiki": False,
        "has_projects": False,
        "has_downloads": True,
        "archived": False,
        "disabled": False,
        "visibility": "private" if repo.private else "public",
        "permissions": {"admin": True, "push": True, "pull": True},
    }

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

# --------------------------------------------------------------------------- HTTP surface

TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
REQUEST_COUNTER = [0]


def read_body(handler):
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks = []
        while True:
            line = handler.rfile.readline(65536)
            if not line:
                break
            size = int(line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                handler.rfile.readline(65536)
                break
            chunks.append(handler.rfile.read(size))
            handler.rfile.read(2)
        return b"".join(chunks)
    length = handler.headers.get("Content-Length")
    if not length:
        return b""
    try:
        return handler.rfile.read(int(length))
    except (TypeError, ValueError):
        return b""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "forge/1.0"
    sys_version = ""
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def do_DELETE(self):
        self.handle_any("DELETE")

    def do_HEAD(self):
        self.handle_any("HEAD")

    # -- helpers ---------------------------------------------------------

    def request_id(self):
        REQUEST_COUNTER[0] += 1
        return "%08X:%08X" % (os.getpid() & 0xFFFFFFFF, REQUEST_COUNTER[0])

    def authorized(self):
        header = self.headers.get("Authorization", "")
        if not header:
            return False
        parts = header.split(None, 1)
        token = parts[1].strip() if len(parts) > 1 else ""
        if TOKEN == "":
            return True
        return token == TOKEN

    def base_url(self):
        host = self.headers.get("Host") or "127.0.0.1"
        return "http://" + host

    def send_json(self, status, payload, headers=None, extra=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-GitHub-Request-Id", self.request_id())
        self.send_header("X-OAuth-Scopes", "repo, read:org")
        self.send_header("X-Accepted-OAuth-Scopes", "")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_bytes(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-GitHub-Request-Id", self.request_id())
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_error_json(self, status, message, headers=None):
        self.send_json(status, {"message": message, "documentation_url": "https://docs.github.com/rest"},
                       headers)

    # -- routing ---------------------------------------------------------

    def handle_any(self, method):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = {}
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                query.setdefault(key, value)
            body_raw = read_body(self)
        except Exception:
            try:
                self.send_error_json(400, "Bad Request")
            except Exception:
                pass
            return

        try:
            self.route(method, path, query, body_raw)
        except HttpError as err:
            try:
                self.send_error_json(err.status, err.message, err.headers)
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_error_json(500, "Internal Server Error")
            except Exception:
                pass

    def route(self, method, path, query, body_raw):
        if path == "/healthz":
            self.send_json(200, {"ok": True})
            return

        if path.startswith("/api/v3"):
            if not self.authorized():
                self.send_error_json(401, "Requires authentication")
                return
            body = None
            if body_raw:
                try:
                    body = json.loads(body_raw)
                except ValueError:
                    self.send_error_json(400, "Problems parsing JSON")
                    return
            parts = [segment for segment in path[len("/api/v3") :].split("/") if segment]
            status, payload, headers = rest_dispatch(method, parts, query, body, self.base_url())
            self.send_json(status, payload, headers)
            return

        if path == "/api/graphql":
            if not self.authorized():
                self.send_error_json(401, "Requires authentication")
                return
            try:
                body = json.loads(body_raw) if body_raw else None
            except ValueError:
                self.send_error_json(400, "Problems parsing JSON")
                return
            status, payload = graphql_dispatch(body, self.base_url())
            self.send_json(status, payload)
            return

        if path == "/mcp" or path.startswith("/mcp/"):
            self.mcp(method, body_raw)
            return

        if path.endswith(".git") or ".git/" in path:
            self.git(method, path, query, body_raw)
            return

        self.send_error_json(404, "Not Found")

# --------------------------------------------------------------------------- placeholders
# Filled in by the chunks that follow; assigned onto Handler at import time.


def _git_unimplemented(self, method, path, query, body_raw):
    self.send_error_json(404, "Not Found")


def _mcp_unimplemented(self, method, body):
    self.send_error_json(404, "Not Found")


def _graphql_unimplemented(body):
    return 200, {"data": None, "errors": [{"message": "not implemented"}]}


Handler.git = _git_unimplemented
Handler.mcp = _mcp_unimplemented
graphql_dispatch = _graphql_unimplemented

# --------------------------------------------------------------------------- git smart HTTP

FLUSH = b"0000"
DELIM = b"0001"
SIDEBAND_MAX = 65515
UPLOAD_CAPS_V0 = ("multi_ack_detailed side-band-64k thin-pack ofs-delta shallow "
                  "no-progress include-tag agent=forge/1.0 object-format=sha1")
RECEIVE_CAPS = "report-status delete-refs side-band-64k ofs-delta quiet agent=forge/1.0"


def pkt(data):
    if isinstance(data, str):
        data = data.encode()
    return b"%04x" % (len(data) + 4) + data


def read_pkt_stream(body, start=0):
    """[(kind, payload)] plus the offset just past the final flush."""
    out = []
    i = start
    n = len(body)
    while i + 4 <= n:
        header = body[i : i + 4]
        try:
            length = int(header, 16)
        except ValueError:
            raise HttpError(400, "bad pkt-line length")
        if length == 0:
            out.append(("flush", b""))
            i += 4
            break
        if length == 1:
            out.append(("delim", b""))
            i += 4
            continue
        if length == 2:
            out.append(("response-end", b""))
            i += 4
            continue
        if length < 4 or i + length > n:
            raise HttpError(400, "truncated pkt-line")
        out.append(("data", body[i + 4 : i + length]))
        i += length
    return out, i


def pkt_lines(lines):
    out = bytearray()
    for line in lines:
        if line == "flush":
            out += FLUSH
        elif line == "delim":
            out += DELIM
        else:
            out += pkt(line)
    return bytes(out)


def sideband_stream(payload, band=1):
    out = bytearray()
    for start in range(0, len(payload), SIDEBAND_MAX):
        out += pkt(bytes([band]) + payload[start : start + SIDEBAND_MAX])
    return bytes(out)


# -- pack writing -----------------------------------------------------------


def pack_object_header(kind, size):
    order = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
    first = (order[kind] << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(object_ids):
    body = bytearray()
    count = 0
    for oid in object_ids:
        kind, payload = STORE.get(oid)
        body += pack_object_header(kind, len(payload))
        body += zlib.compress(payload, 6)
        count += 1
    header = b"PACK" + struct.pack(">II", 2, count)
    pack = header + bytes(body)
    return pack + hashlib.sha1(pack).digest()


PACK_CACHE = {}
PACK_FRAME_CACHE = {}
PACK_CACHE_LOCK = threading.Lock()
PACK_CACHE_MAX = 48


def cached_pack(ids):
    """write_pack, memoised by object set.

    A pack of a fixed, sorted object set is immutable -- objects are content
    addressed and never change -- so the bytes can be kept.  The clone workload
    is 50 concurrent shallow clones of the same tip: without this every one of
    them compresses the same pack, and the GIL turns 50 parallel requests into
    50 serial ones.
    """
    key = tuple(ids)
    hit = PACK_CACHE.get(key)
    if hit is not None:
        return hit
    with PACK_CACHE_LOCK:
        hit = PACK_CACHE.get(key)
        if hit is None:
            hit = write_pack(list(key))
            if len(PACK_CACHE) < PACK_CACHE_MAX:
                PACK_CACHE[key] = hit
    return hit


def cached_pack_framed(ids):
    """Return (pack, sideband_stream(pack)), both memoised by object set.

    The framed bytes are a pure function of the pack, and the pack is a pure
    function of the sorted object set, so the sideband re-framing can be kept
    alongside it.  Same key, same lock, same bound: a repeated fetch of one
    object set pays neither the compression nor the re-framing again, and two
    threads racing on a cold entry cannot both build it.
    """
    key = tuple(ids)
    pack = PACK_CACHE.get(key)
    framed = PACK_FRAME_CACHE.get(key)
    if pack is not None and framed is not None:
        return pack, framed
    with PACK_CACHE_LOCK:
        pack = PACK_CACHE.get(key)
        if pack is None:
            pack = write_pack(list(key))
            if len(PACK_CACHE) < PACK_CACHE_MAX:
                PACK_CACHE[key] = pack
        framed = PACK_FRAME_CACHE.get(key)
        if framed is None:
            framed = sideband_stream(pack)
            if len(PACK_FRAME_CACHE) < PACK_CACHE_MAX:
                PACK_FRAME_CACHE[key] = framed
    return pack, framed


def apply_delta(base, delta):
    pos = 0

    def varint():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    src_size = varint()
    dst_size = varint()
    out = bytearray()
    while pos < len(delta):
        opcode = delta[pos]
        pos += 1
        if opcode & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if opcode & (1 << bit):
                    offset |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if opcode & (1 << (4 + bit)):
                    size |= delta[pos] << (8 * bit)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[offset : offset + size]
        elif opcode:
            out += delta[pos : pos + opcode]
            pos += opcode
        else:
            raise HttpError(400, "bad delta opcode")
    if len(out) != dst_size:
        raise HttpError(400, "delta size mismatch")
    return bytes(out)


def read_pack(pack):
    """Store every object in a pack; returns the object ids it held."""
    if len(pack) < 32 or pack[:4] != b"PACK":
        raise HttpError(400, "not a pack")
    version, count = struct.unpack(">II", pack[4:12])
    pos = 12
    order = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
    resolved = {}
    pending = []
    oids = []
    offsets = {}
    for _ in range(count):
        start = pos
        byte = pack[pos]
        pos += 1
        kind_id = (byte >> 4) & 7
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = pack[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        base_ref = None
        if kind_id == 6:
            byte = pack[pos]
            pos += 1
            offset = byte & 0x7F
            while byte & 0x80:
                byte = pack[pos]
                pos += 1
                offset = ((offset + 1) << 7) | (byte & 0x7F)
            base_ref = ("ofs", start - offset)
        elif kind_id == 7:
            base_ref = ("ref", pack[pos : pos + 20].hex())
            pos += 20
        data = zlib.decompressobj()
        raw = data.decompress(pack[pos:])
        pos += len(pack[pos:]) - len(data.unused_data)
        if kind_id in order:
            sha = STORE.put(order[kind_id], raw)
            resolved[start] = (order[kind_id], raw)
            oids.append(sha)
        else:
            pending.append((start, base_ref, raw))
    for start, base_ref, raw in pending:
        if base_ref[0] == "ofs":
            base = resolved.get(base_ref[1])
        else:
            base_sha = base_ref[1]
            base = None
            if STORE.has(base_sha):
                base = STORE.get(base_sha)
            if base is None:
                for key, value in resolved.items():
                    if object_sha(value[0], value[1]) == base_sha:
                        base = value
                        break
        if base is None:
            raise HttpError(400, "missing delta base")
        payload = apply_delta(base[1], raw)
        sha = STORE.put(base[0], payload)
        resolved[start] = (base[0], payload)
        oids.append(sha)
    return oids


# -- closures ---------------------------------------------------------------


def tree_closure(tree_oid, out):
    """Add a tree and everything under it (subtrees and blobs); never follows commits."""
    stack = [tree_oid]
    while stack:
        oid = stack.pop()
        if oid in out or not STORE.has(oid):
            continue
        kind, payload = STORE.get(oid)
        out.add(oid)
        if kind == "tree":
            for _mode, _name, sha in parse_tree(payload):
                stack.append(sha)


def shallow_objects(commit_oids):
    """The objects a shallow client holds for these commits: the commits, their trees, their blobs."""
    out = set()
    for oid in commit_oids:
        if not STORE.has(oid):
            continue
        kind, payload = STORE.get(oid)
        out.add(oid)
        if kind == "commit":
            tree_closure(parse_commit(payload)[0], out)
    return out


def commit_closure(commit_shas):
    """Every object id reachable from these commits (commits, trees, blobs, tags)."""
    seen = set()
    out = set()
    stack = list(commit_shas)
    while stack:
        oid = stack.pop()
        if oid in seen or not STORE.has(oid):
            continue
        seen.add(oid)
        kind, payload = STORE.get(oid)
        out.add(oid)
        if kind == "commit":
            tree, parents, _message, _a, _c = parse_commit(payload)
            stack.extend(parents)
            stack.append(tree)
        elif kind == "tree":
            for mode, _name, sha in parse_tree(payload):
                stack.append(sha)
        elif kind == "tag":
            target = payload.decode("utf-8", "replace").split("object ")[1].split("\n")[0].strip()
            stack.append(target)
    return out


def commit_ancestors(commit_shas):
    seen = set()
    stack = list(commit_shas)
    while stack:
        oid = stack.pop()
        if oid in seen or not STORE.has(oid):
            continue
        kind, payload = STORE.get(oid)
        if kind != "commit":
            continue
        seen.add(oid)
        stack.extend(parse_commit(payload)[1])
    return seen


def depth_commits(wants, depth):
    """Commits within `depth` generations of the wants (wants are generation 1)."""
    levels = {}
    frontier = [(w, 1) for w in wants]
    while frontier:
        oid, generation = frontier.pop(0)
        if oid in levels or not STORE.has(oid):
            continue
        if STORE.get(oid)[0] != "commit":
            continue
        levels[oid] = generation
        if generation >= depth:
            continue
        for parent in parse_commit(STORE.get(oid)[1])[1]:
            if parent not in levels:
                frontier.append((parent, generation + 1))
    return levels


def tag_target(oid):
    if not STORE.has(oid):
        return None
    kind, payload = STORE.get(oid)
    if kind != "tag":
        return None
    for line in payload.decode("utf-8", "replace").split("\n"):
        if line.startswith("object "):
            return line[7:].strip()
    return None


# -- request handling -------------------------------------------------------


def split_git_path(path):
    parts = [segment for segment in path.split("/") if segment]
    if len(parts) < 3:
        return None
    if not parts[1].endswith(".git"):
        return None
    return parts[0], parts[1][: -len(".git")], "/".join(parts[2:])


def git_dispatch(self, method, path, query, body_raw):
    split = split_git_path(path)
    if split is None:
        self.send_error_json(404, "Not Found")
        return
    owner, name, rest = split
    repo = get_repo(owner, name)
    if repo is None or repo.owner.lower() != VIEWER_LOGIN:
        self.send_error_json(404, "Not Found")
        return

    if rest == "info/refs" and method == "GET":
        service = query.get("service", "")
        if service not in ("git-upload-pack", "git-receive-pack"):
            self.send_error_json(403, "Smart HTTP is required")
            return
        body = advertise(repo, service, self.headers.get("Git-Protocol", ""))
        self.send_bytes(200, body, "application/x-%s-advertisement" % service,
                        {"Cache-Control": "no-cache"})
        return

    if rest == "git-upload-pack" and method == "POST":
        try:
            body = handle_upload_pack(repo, body_raw)
        except HttpError as err:
            self.send_error_json(err.status, err.message)
            return
        self.send_bytes(200, body, "application/x-git-upload-pack-result", {"Cache-Control": "no-cache"})
        return

    if rest == "git-receive-pack" and method == "POST":
        try:
            body = handle_receive_pack(repo, body_raw)
        except HttpError as err:
            self.send_error_json(err.status, err.message)
            return
        self.send_bytes(200, body, "application/x-git-receive-pack-result", {"Cache-Control": "no-cache"})
        return

    self.send_error_json(404, "Not Found")


def head_oid(repo):
    return repo.tip()


def advertise(repo, service, protocol_header):
    if service == "git-upload-pack":
        if "version=2" in protocol_header:
            return pkt_lines([
                "# service=git-upload-pack\n", "flush",
                "version 2\n",
                "agent=forge/1.0\n",
                "ls-refs=unborn\n",
                "fetch=shallow\n",
                "object-format=sha1\n",
                "flush",
            ])
        lines = ["# service=git-upload-pack\n", "flush"]
        refs = sorted((ref, sha) for ref, sha in repo.refs.items())
        if not refs:
            lines.append(ZERO_OID + " capabilities^{}\x00" + UPLOAD_CAPS_V0 + "\n")
        else:
            first = True
            for ref, sha in refs:
                if first:
                    lines.append("%s %s\x00%s\n" % (sha, ref, UPLOAD_CAPS_V0))
                    first = False
                else:
                    lines.append("%s %s\n" % (sha, ref))
            tip = repo.tip()
            if tip:
                lines.append("%s HEAD\n" % tip)
        lines.append("flush")
        return pkt_lines(lines)

    lines = ["# service=git-receive-pack\n", "flush"]
    refs = sorted((ref, sha) for ref, sha in repo.refs.items())
    if not refs:
        lines.append(ZERO_OID + " capabilities^{}\x00" + RECEIVE_CAPS + "\n")
    else:
        first = True
        for ref, sha in refs:
            if first:
                lines.append("%s %s\x00%s\n" % (sha, ref, RECEIVE_CAPS))
                first = False
            else:
                lines.append("%s %s\n" % (sha, ref))
    lines.append("flush")
    return pkt_lines(lines)


def handle_upload_pack(repo, body_raw):
    commands, offset = read_pkt_stream(body_raw)
    command = ""
    wants = []
    haves = []
    shallow = []
    depth = None
    prefixes = []
    peel = False
    symrefs = False
    unborn = False
    for kind, payload in commands:
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("command="):
            command = line[len("command=") :]
        elif line.startswith("want "):
            wants.append(line[5:].strip())
        elif line.startswith("have "):
            haves.append(line[5:].strip())
        elif line.startswith("shallow "):
            shallow.append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                depth = None
        elif line.startswith("ref-prefix "):
            prefixes.append(line[len("ref-prefix ") :].strip())
        elif line == "peel":
            peel = True
        elif line == "symrefs":
            symrefs = True
        elif line == "unborn":
            unborn = True

    if command == "ls-refs":
        return pkt_lines(ls_refs_lines(repo, prefixes, peel, symrefs, unborn))
    if command == "fetch" or (command == "" and wants):
        return fetch_lines(repo, wants, haves, shallow, depth)
    raise HttpError(400, "unknown upload-pack command")


def ls_refs_lines(repo, prefixes, peel, symrefs, unborn):
    def keep(name):
        return not prefixes or any(name.startswith(prefix) for prefix in prefixes)

    out = []
    tip = repo.tip()
    if keep("HEAD"):
        if tip is None and unborn:
            out.append("unborn HEAD symref-target:refs/heads/%s\n" % repo.default_branch)
        elif tip is not None:
            line = "%s HEAD" % tip
            if symrefs:
                line += " symref-target:refs/heads/%s" % repo.default_branch
            out.append(line + "\n")
    for ref in sorted(repo.refs):
        if not keep(ref):
            continue
        sha = repo.refs[ref]
        line = "%s %s" % (sha, ref)
        if peel:
            target = tag_target(sha)
            if target:
                line += " peeled:" + target
        out.append(line + "\n")
    out.append("flush")
    return out


def fetch_lines(repo, wants, haves, shallow, depth):
    wants = [w for w in wants if STORE.has(w)]
    if not wants:
        raise HttpError(400, "fetch without a valid want")
    out = ["acknowledgments\n", "NAK\n", "delim"]

    if depth is not None:
        levels = depth_commits(wants, depth)
        boundary = []
        for oid, generation in levels.items():
            if generation >= depth:
                parents = parse_commit(STORE.get(oid)[1])[1]
                if parents:
                    boundary.append(oid)
        unshallow = [oid for oid in shallow if oid in levels and oid not in boundary]
        if boundary or unshallow:
            out.append("shallow-info\n")
            for oid in boundary:
                out.append("shallow %s\n" % oid)
            for oid in unshallow:
                out.append("unshallow %s\n" % oid)
            out.append("delim")
        known = shallow_objects([h for h in haves if STORE.has(h)])
        send = shallow_objects(list(levels))
        send -= known
        _pack, framed = cached_pack_framed(sorted(send))
    else:
        want_set = commit_closure(wants)
        have_set = commit_closure([h for h in haves if STORE.has(h)])
        _pack, framed = cached_pack_framed(sorted(want_set - have_set))

    out.append("packfile\n")
    head = pkt_lines(out)
    return head + framed + FLUSH


def handle_receive_pack(repo, body_raw):
    commands, offset = read_pkt_stream(body_raw)
    updates = []
    for kind, payload in commands:
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace")
        nul = line.find("\x00")
        if nul >= 0:
            line = line[:nul]
        line = line.rstrip("\n")
        fields = line.split(" ")
        if len(fields) < 3:
            raise HttpError(400, "bad receive-pack command")
        updates.append((fields[0].lower(), fields[1].lower(), fields[2]))

    if offset < len(body_raw):
        read_pack(body_raw[offset:])

    report = ["unpack ok\n"]
    for old_oid, new_oid, ref in updates:
        current = repo.refs.get(ref)
        if new_oid == ZERO_OID:
            if current is None:
                report.append("ng %s delete-current\n" % ref)
            else:
                del repo.refs[ref]
                report.append("ok %s\n" % ref)
            continue
        if not STORE.has(new_oid):
            report.append("ng %s missing-object\n" % ref)
            continue
        if current is None:
            if old_oid != ZERO_OID:
                report.append("ng %s stale-info\n" % ref)
                continue
            repo.refs[ref] = new_oid
            report.append("ok %s\n" % ref)
            continue
        if old_oid != current:
            report.append("ng %s stale-info\n" % ref)
            continue
        if new_oid != current:
            lineage = commit_ancestors([new_oid])
            if current not in lineage:
                report.append("ng %s non-fast-forward\n" % ref)
                continue
        repo.refs[ref] = new_oid
        report.append("ok %s\n" % ref)
    report.append("flush")
    inner = pkt_lines(report)
    return sideband_stream(inner) + FLUSH


Handler.git = git_dispatch

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

# --------------------------------------------------------------------------- lifecycle


def post_register(env_url, port):
    body = json.dumps({"port": port}).encode()
    request = urllib.request.Request(env_url.rstrip("/") + "/register", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read()


def poll_done(env_url):
    while True:
        try:
            request = urllib.request.Request(env_url.rstrip("/") + "/done", method="GET")
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read() or b"{}")
            if payload.get("done") is True:
                return
        except Exception:
            pass
        time.sleep(0.05)


class ForgeServer(ThreadingHTTPServer):
    """Threading server with a listen backlog that survives 50 simultaneous clones.

    request_queue_size must be a class attribute: socketserver calls listen()
    during __init__, so assigning it on the instance afterwards leaves the
    kernel backlog at the default 5.  With 5, a round of 50 concurrent clone
    connections loses SYNs and the client's TCP retries turn a 250 ms round
    into 4 s.
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 512


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "")
    server = ForgeServer(("0.0.0.0", int(os.environ.get("PORT", "0"))), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()

    if env_url:
        for _ in range(20):
            try:
                status, _body = post_register(env_url, port)
                if status in (200, 201, 204):
                    break
            except Exception:
                pass
            time.sleep(0.1)
        poll_done(env_url)
    else:
        sys.stdout.write("listening on %d\n" % port)
        sys.stdout.flush()
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
