
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
