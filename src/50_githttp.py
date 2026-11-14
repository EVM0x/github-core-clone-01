
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
        pack = cached_pack(sorted(send))
    else:
        want_set = commit_closure(wants)
        have_set = commit_closure([h for h in haves if STORE.has(h)])
        pack = cached_pack(sorted(want_set - have_set))

    out.append("packfile\n")
    head = pkt_lines(out)
    return head + sideband_stream(pack) + FLUSH


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
