
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
