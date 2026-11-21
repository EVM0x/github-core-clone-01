"""Tests for the forge's pure surfaces.

The store is a module-level singleton, so every test builds its own repository
with a unique name and reads it back through the same entry points the suite
uses: graphql_dispatch for GraphQL, mcp_handle for MCP, object_sha for git.

Run: python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as forge  # noqa: E402


def gql(query, variables=None, base_url="http://127.0.0.1:9"):
    status, payload = forge.graphql_dispatch(
        {"query": query, "variables": variables or {}}, base_url)
    assert status == 200, status
    return payload


def mcp(method, params=None, request_id=1):
    return forge.mcp_handle({"jsonrpc": "2.0", "id": request_id,
                             "method": method, "params": params or {}})


class Fixture(unittest.TestCase):
    """One fresh repository per test, with the issues a test asks for."""

    counter = 0

    def make_repo(self, issues=()):
        Fixture.counter += 1
        name = "t%d" % Fixture.counter
        forge.create_repo({"name": name})
        repo = forge.get_repo(forge.VIEWER_LOGIN, name)
        numbers = []
        for title in issues:
            _status, issue, _headers = forge.create_issue(repo, {"title": title})
            numbers.append(issue["number"])
        return repo, numbers


class GitObjectTest(unittest.TestCase):
    def test_a_blob_hashes_the_way_git_hashes_it(self):
        self.assertEqual(forge.object_sha("blob", b"hello\n"),
                         "ce013625030ba8dba906f756967f9e9ca394464a")

    def test_the_empty_blob_and_tree_match_git(self):
        self.assertEqual(forge.object_sha("blob", b""),
                         "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        self.assertEqual(forge.object_sha("tree", b""),
                         "4b825dc642cb6eb9a060e54bf8d69288fbee4904")

    def test_the_header_is_part_of_the_hash(self):
        # The same bytes as a blob and as a tree must not collide.
        self.assertNotEqual(forge.object_sha("blob", b"x"),
                            forge.object_sha("tree", b"x"))


class GraphQLShorthandTest(Fixture):
    def test_an_anonymous_shorthand_query_is_parsed(self):
        payload = gql('{ __type(name: "Issue") { name } }')
        self.assertNotIn("errors", payload, payload)
        self.assertEqual(payload["data"]["__type"]["name"], "Issue")

    def test_introspection_lists_the_fields_the_suite_asks_for(self):
        payload = gql('{ __type(name: "Issue") { fields { name } } }')
        names = {field["name"] for field in payload["data"]["__type"]["fields"]}
        for expected in ("id", "number", "title", "state", "body"):
            self.assertIn(expected, names)

    def test_a_named_query_still_works(self):
        payload = gql('query Named { __type(name: "Issue") { name } }')
        self.assertEqual(payload["data"]["__type"]["name"], "Issue")

    def test_two_documents_in_one_body_answer_in_order(self):
        status, payload = forge.graphql_dispatch([
            {"query": '{ __type(name: "Issue") { name } }'},
            {"query": '{ __type(name: "Repository") { name } }'},
        ], "")
        self.assertEqual(status, 200)
        self.assertEqual(payload[0]["data"]["__type"]["name"], "Issue")
        self.assertEqual(payload[1]["data"]["__type"]["name"], "Repository")


class FragmentTest(Fixture):
    def test_a_fragment_resolves_inside_its_own_document(self):
        payload = gql('query { __type(name: "Issue") { ...F } } fragment F on __Type { name }')
        self.assertNotIn("errors", payload, payload)
        self.assertEqual(payload["data"]["__type"]["name"], "Issue")

    def test_a_fragment_does_not_leak_into_the_next_request(self):
        # Define a fragment, then send the same document with the definition cut
        # off. An undefined fragment expands to no selections at all, so the
        # second answer must be the whole type rather than the one field the
        # first request happened to define.
        gql('query { __type(name: "Issue") { ...Leak } } fragment Leak on __Type { name }')
        payload = gql('query { __type(name: "Issue") { ...Leak } }')
        got = payload["data"]["__type"]
        self.assertNotEqual(sorted(got), ["name"], got)
        self.assertIn("fields", got)


class OrderByTest(Fixture):
    def setUp(self):
        self.repo, self.numbers = self.make_repo(["one", "two", "three"])

    def query(self, direction):
        return (
            'query { repository(owner: "%s", name: "%s") { issues('
            'orderBy: {field: CREATED_AT, direction: %s}) { nodes { number } } } }'
            % (forge.VIEWER_LOGIN, self.repo.name, direction)
        )

    def numbers_of(self, direction):
        payload = gql(self.query(direction))
        self.assertNotIn("errors", payload, payload)
        return [node["number"] for node in payload["data"]["repository"]["issues"]["nodes"]]

    def test_created_at_desc_is_newest_first(self):
        self.assertEqual(self.numbers_of("DESC"), sorted(self.numbers, reverse=True))

    def test_created_at_asc_is_oldest_first(self):
        self.assertEqual(self.numbers_of("ASC"), sorted(self.numbers))

    def test_states_open_omits_closed_issues(self):
        forge.issues_rest(self.repo, "PATCH", [str(self.numbers[0])], {}, {"state": "closed"})
        payload = gql(
            'query { repository(owner: "%s", name: "%s") { issues(states: [OPEN]) '
            '{ nodes { number } } } }' % (forge.VIEWER_LOGIN, self.repo.name))
        seen = [node["number"] for node in payload["data"]["repository"]["issues"]["nodes"]]
        self.assertNotIn(self.numbers[0], seen)
        self.assertEqual(len(seen), 2)


class TotalCountTest(Fixture):
    def test_total_count_is_the_whole_connection_not_the_page(self):
        self.repo, numbers = self.make_repo(["a", "b", "c"])
        payload = gql(
            'query { repository(owner: "%s", name: "%s") { issues(first: 1) '
            '{ totalCount nodes { number } } } }' % (forge.VIEWER_LOGIN, self.repo.name))
        conn = payload["data"]["repository"]["issues"]
        self.assertEqual(len(conn["nodes"]), 1)
        self.assertEqual(conn["totalCount"], 3)


class TimestampTest(Fixture):
    def test_created_and_updated_are_not_the_same_field(self):
        self.repo, numbers = self.make_repo(["x"])
        issue = forge.find_issue(self.repo, numbers[0])
        issue["updated"] = issue["created"] + 3600
        payload = gql(
            'query { repository(owner: "%s", name: "%s") { issue(number: %d) '
            '{ createdAt updatedAt } } }' % (forge.VIEWER_LOGIN, self.repo.name, numbers[0]))
        got = payload["data"]["repository"]["issue"]
        self.assertNotEqual(got["createdAt"], got["updatedAt"])

    def test_timestamps_are_real_dates_not_the_far_future(self):
        # A counter was being added to an absolute epoch, which produced 5252.
        self.repo, numbers = self.make_repo(["y"])
        payload = gql(
            'query { repository(owner: "%s", name: "%s") { issue(number: %d) '
            '{ createdAt } } }' % (forge.VIEWER_LOGIN, self.repo.name, numbers[0]))
        stamp = payload["data"]["repository"]["issue"]["createdAt"]
        self.assertTrue(stamp.startswith("20"), stamp)
        self.assertLess(int(stamp[:4]), 2100)

    def test_closed_at_is_only_set_once_closed(self):
        self.repo, numbers = self.make_repo(["z"])
        query = ('query { repository(owner: "%s", name: "%s") { issue(number: %d) '
                 '{ closedAt state } } }' % (forge.VIEWER_LOGIN, self.repo.name, numbers[0]))
        self.assertIsNone(gql(query)["data"]["repository"]["issue"]["closedAt"])
        forge.issues_rest(self.repo, "PATCH", [str(numbers[0])], {}, {"state": "closed"})
        after = gql(query)["data"]["repository"]["issue"]
        self.assertEqual(after["state"], "CLOSED")
        self.assertTrue(after["closedAt"].startswith("20"))


class McpDiscoverTest(unittest.TestCase):
    def test_server_discover_lists_supported_revisions(self):
        response = mcp("server/discover")
        self.assertNotIn("error", response, response)
        versions = response["result"]["supportedVersions"]
        self.assertTrue(versions)
        for version in versions:
            self.assertRegex(version, r"^\d{4}-\d{2}-\d{2}$")


class McpToolNameTest(Fixture):
    def test_a_tool_name_with_a_server_prefix_resolves(self):
        self.repo, _ = self.make_repo([])
        response = mcp("tools/call", {"name": "github__get_repo",
                                      "arguments": {"owner": forge.VIEWER_LOGIN,
                                                    "repo": self.repo.name}})
        self.assertNotIn("error", response, response)

    def test_a_tool_name_is_matched_case_insensitively(self):
        self.repo, _ = self.make_repo([])
        response = mcp("tools/call", {"name": "GitHub__Get_Repo",
                                      "arguments": {"owner": forge.VIEWER_LOGIN,
                                                    "repo": self.repo.name}})
        self.assertNotIn("error", response, response)

    def test_an_unknown_tool_is_still_refused(self):
        response = mcp("tools/call", {"name": "github__no_such_tool", "arguments": {}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_a_plain_name_is_unchanged(self):
        self.repo, _ = self.make_repo([])
        response = mcp("tools/call", {"name": "get_repo",
                                      "arguments": {"owner": forge.VIEWER_LOGIN,
                                                    "repo": self.repo.name}})
        self.assertNotIn("error", response, response)


if __name__ == "__main__":
    unittest.main()
