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
