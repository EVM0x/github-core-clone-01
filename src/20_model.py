
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
